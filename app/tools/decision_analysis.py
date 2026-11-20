"""Decision calculations with explicit user assumptions and liquidity boundaries."""

from collections import defaultdict
from datetime import date
from statistics import median

from app.tools.financial_analysis import (
    _anomalies, _buckets, _date, _forecast, _nonrecurring_income,
    _round_money, _series, _source, _totals, forecast_scope, shift_month,
)

from app.tools.source_analysis import money, observed_source_key, source_aliases, source_rows


def _source_parts(orders, rows, months, metric, source):
    exact = [r for r in rows if source in (r["sourceKey"], r["source"], r["member"],
             r["member"] + "的" + r["source"], r["member"] + r["source"])]
    matched = exact or [r for r in rows if source in r["source"]]
    if matched:
        return [(row, row["monthlyAmounts"]) for row in matched]

    # Restrict original remark/category matches to their contribution inside each semantic group.
    components = {key: row for row in rows for key in row["observedSourceKeys"]}
    amounts = defaultdict(lambda: defaultdict(float))
    for order in orders:
        month = _date(order).strftime("%Y-%m")
        amount = float(order["money"])
        if month not in months or amount == 0 or (amount > 0) != (metric == "income"):
            continue
        if source not in _source(order) and source not in str(order.get("orderRemark") or ""):
            continue
        component = components.get(observed_source_key(order))
        if component:
            amounts[component["sourceKey"]][month] += abs(amount)
    return [(row, monthly) for row in rows if (monthly := amounts.get(row["sourceKey"]))]


def _parts_baseline(parts, months, metric):
    if metric == "expense":
        # Keep the expense estimator: take the median of the selected monthly totals.
        return money(median(sum(values.get(month, 0) for _, values in parts) for month in months))
    estimates = [row["estimate"] if values is row["monthlyAmounts"]
                 else money(median(values.get(month, 0) for month in months))
                 for row, values in parts]
    return money(sum(estimates))


def _raw_source_baseline(orders, months, metric, source):
    amounts = defaultdict(float)
    matched = set()
    for order in orders:
        month = _date(order).strftime("%Y-%m")
        positive = float(order["money"]) > 0
        if month not in months or positive != (metric == "income"):
            continue
        if positive and _nonrecurring_income(order):
            continue
        if source in _source(order) or source in str(order.get("orderRemark") or ""):
            amounts[month] += abs(float(order["money"]))
            matched.add(_source(order))
    return money(median(amounts.get(month, 0) for month in months)), sorted(matched)


def apply_changes(orders, income, expense, changes, *, source_groups=None):
    totals = {"income": income["estimate"], "expense": expense["estimate"]}
    assumptions = []
    used = defaultdict(float)
    months = income["baselineMonths"]
    rows_by_metric = {}
    if "sourceForecasts" in income:
        rows_by_metric["income"] = [r for r in income["sourceForecasts"] if not r["excluded"]]
    if any(change["metric"] == "expense" and change.get("source") for change in changes):
        expenses = [order for order in orders if float(order["money"]) < 0
                    and _date(order).strftime("%Y-%m") in months]
        rows_by_metric["expense"] = source_rows(expenses, source_aliases(orders, source_groups))
    for change in changes:
        metric, source = change["metric"], str(change.get("source") or "").strip()
        base = income['estimate'] if metric == 'income' else expense['estimate']
        matched, parts = [], []
        if source:
            if metric in rows_by_metric:
                parts = _source_parts(orders, rows_by_metric[metric], months, metric, source)
                base = _parts_baseline(parts, months, metric)
                matched = sorted({row["source"] for row, _ in parts})
            else:
                base, matched = _raw_source_baseline(orders, months, metric, source)
            if not matched and (change.get("percent") is not None or (change.get("amount") or 0) < 0):
                return {"sufficientData": False, "missingInputs": ["source"],
                        "reason": f"历史账单未能定位来源“{source}”，不能用全部收支代替该来源。"}
        delta = (base * change["percent"] / 100 if change.get("percent") is not None
                 else change["amount"] * change.get("count", 1))
        delta = _round_money(delta)
        if source and delta < -base:
            return {"sufficientData": False, "reason": "所设减少额超过该来源的历史基准，请确认变化条件。"}
        if delta < 0 and parts:
            # Apply the requested fraction to the matching records in each baseline month.
            # A household alias and a qualified member alias can refer to the same spend.
            fraction = -delta / base
            for row, amounts in parts:
                for month in months:
                    key = (row["sourceKey"], month)
                    used[key] += fraction * amounts.get(month, 0)
                    if money(used[key]) > row["monthlyAmounts"].get(month, 0):
                        return {"sufficientData": False, "reason": "多项减少条件重叠，累计减少额超过该来源的历史贡献。"}
        totals[metric] += delta
        assumptions.append({**change, "sourceBaseline": _round_money(base),
                            "matchedSources": matched, "matchedSourceKeys": [row["sourceKey"] for row, _ in parts],
                            "totalChange": delta})
    if min(totals.values()) < 0:
        return {"sufficientData": False, "reason": "变化会使收入或支出变成负数，请确认是否涉及退款或不同统计口径。"}
    return {"sufficientData": True, "totals": {key: _round_money(value) for key, value in totals.items()},
            "assumptions": assumptions}


def baseline(orders, as_of, changes=None, target_month=None, **kwargs):
    scope = {"target_month": target_month, **forecast_scope(kwargs)}
    income = _forecast(orders, as_of=as_of, metric="income", **scope)
    expense = _forecast(orders, as_of=as_of, metric="expense", **scope)
    if not income["sufficientData"]:
        return {"sufficientData": False, "reason": "需要至少两个有记录的完整月份作为历史依据。"}
    adjusted = apply_changes(orders, income, expense, changes or [], source_groups=scope.get("source_groups"))
    if not adjusted["sufficientData"]:
        return adjusted
    totals = adjusted["totals"]
    net = _round_money(totals["income"] - totals["expense"])
    net_delta = net - (income["estimate"] - expense["estimate"])
    return {"sufficientData": True, "income": totals["income"], "expense": totals["expense"], "net": net,
            "lowerNet": money(income["lower"] - expense["upper"] + net_delta),
            "upperNet": money(income["upper"] - expense["lower"] + net_delta),
            "rangeMethod": "各收入来源情景界与支出上下界组合，按本次假设的净变动金额平移。",
            "assumptions": adjusted["assumptions"], "baselineMonths": income["baselineMonths"],
            "incomeRange": [income['lower'], income['upper']], "expenseRange": [expense['lower'], expense['upper']],
            "incomeSources": [row['source'] for row in income['sources']['income'] if not row['possibleNonrecurring']],
            "latestCompleteChange": income["latestCompleteChange"], "limitations": income["limitations"]}


def projection(orders, *, as_of, months=None, starting_balance=None, changes=None, target_month=None, **kwargs):
    values = baseline(orders, as_of, changes, target_month, **forecast_scope(kwargs))
    if not values["sufficientData"]:
        return values
    if months is None:
        return {"sufficientData": False, "missingInputs": ["months"], "baseline": values}
    start = date.fromisoformat(target_month + "-01") if target_month else shift_month(as_of, 1)
    total = _round_money(values["net"] * months)
    return {"sufficientData": True, "months": months, "startMonth": start.strftime("%Y-%m"),
            "endMonth": shift_month(start, months - 1).strftime("%Y-%m"),
            "monthlyIncome": values["income"], "monthlyExpense": values["expense"], "monthlyNet": values["net"],
            "totalIncome": _round_money(values["income"] * months), "totalExpense": _round_money(values["expense"] * months),
            "totalNet": total, "lowerTotalNet": _round_money(values["lowerNet"] * months),
            "upperTotalNet": _round_money(values["upperNet"] * months),
            "projectedBalance": _round_money(starting_balance + total) if starting_balance is not None else None,
            "assumptions": values["assumptions"], "baseline": values,
            "interpretation": "累计新增结余是期间现金流，不是当前存款。各月假设延续同一历史基准；历史情景范围不是置信区间。"}


def income_stability(orders, *, as_of, **kwargs):
    complete = {row['month'] for row in _series(orders, as_of, kwargs.get('window_start'), kwargs.get('window_end')) if not row['partial']}
    history = [row for row in orders if _date(row).strftime('%Y-%m') in complete]
    buckets = _buckets(history)
    earned = {month: [row for row in rows if not (float(row["money"]) > 0 and _nonrecurring_income(row))]
              for month, rows in buckets.items()}
    totals = [_totals(rows)["incomeTotal"] for rows in earned.values()]
    sources = defaultdict(float)
    for rows in earned.values():
        for row in rows:
            if float(row["money"]) > 0:
                sources[_source(row)] += float(row["money"])
    total = sum(sources.values())
    excluded = [row for row in history if float(row["money"]) > 0 and _nonrecurring_income(row)]
    return {"sufficientData": len(totals) >= 2, "observedMonths": list(buckets),
            "incomeBasis": "complete_months_excluding_identified_nonrecurring",
            "includedIncomeTotal": _round_money(total),
            "excludedNonrecurringIncomeCount": len(excluded),
            "excludedNonrecurringIncomeTotal": _round_money(sum(float(row["money"]) for row in excluded)),
            "incomeMin": min(totals) if totals else None, "incomeMax": max(totals) if totals else None,
            "incomeMedian": median(totals) if totals else None,
            "sources": [{"source": key, "amount": _round_money(value),
                         "sharePercent": _round_money(value / total * 100) if total else None}
                        for key, value in sorted(sources.items(), key=lambda item: item[1], reverse=True)],
            "interpretation": "只统计已观察完整月份，并剔除备注规则识别的非持续性入账；未被规则识别不等于持续收入。此口径的波动和集中度不能代表全部入账，也不能证明未来稳定、当前存款或承受时长。"}


ESSENTIAL_WORDS = ("房租", "房贷", "餐饮", "吃饭", "饮食", "交通", "通勤", "医疗", "水电", "燃气", "社保", "学费")


def optimization(orders, *, as_of, monthly_improvement=None, protected_categories=None, **kwargs):
    goal_details = {}
    starting, target, horizon = (kwargs.get(key) for key in ('starting_balance', 'target_amount', 'months'))
    if starting is not None and target is not None and horizon:
        values = baseline(orders, as_of, kwargs.get('changes'), **forecast_scope(kwargs))
        if not values['sufficientData']:
            return values
        required = max(0, (target - starting) / horizon)
        monthly_improvement = max(0, _round_money(required - values['net']))
        goal_details = {'goalMonthlyRequirement': _round_money(required), 'baselineMonthlySaving': values['net']}
    history = [row for row in orders if _date(row) < as_of.replace(day=1)]
    months = list(_buckets(history))[-3:]
    protected = list(protected_categories or [])
    groups = defaultdict(lambda: defaultdict(float))
    excluded = set()
    for row in history:
        month = _date(row).strftime("%Y-%m")
        if month in months and float(row["money"]) < 0:
            category = str(row.get("costType") or "未分类")
            description = category + ' ' + str(row.get('orderRemark') or '')
            if any(word in description for word in protected + list(ESSENTIAL_WORDS)):
                excluded.add(category)
                continue
            groups[category][month] += abs(float(row["money"]))
    candidates = []
    for category, amounts in groups.items():
        value = _round_money(median(amounts.get(month, 0) for month in months))
        if value:
            candidates.append({"category": category, "typicalMonthlySpend": value})
    candidates.sort(key=lambda row: row["typicalMonthlySpend"], reverse=True)
    capacity = _round_money(sum(row["typicalMonthlySpend"] for row in candidates))
    return {"sufficientData": len(months) >= 2, "monthlyImprovement": monthly_improvement, **goal_details,
            "protectedCategories": protected, "excludedCategories": sorted(excluded), "candidates": candidates,
            "reviewableMonthlySpend": capacity,
            "gapBeyondReviewableSpend": max(0, _round_money(monthly_improvement - capacity)) if monthly_improvement is not None else None,
            "interpretation": "候选支出总额是可逐项核查的上界，不代表全部可以取消。按名称暂时保留必要生活类别及用户保护项；分类规则需用户确认。不能承诺不影响生活，不能为了凑目标削减保护项。"}


def purchase(orders, *, as_of, purchase_amount=None, starting_balance=None, reserve_amount=None, **kwargs):
    missing = [key for key, value in (("purchaseAmount", purchase_amount), ("startingBalance", starting_balance)) if value is None]
    values = baseline(orders, as_of, kwargs.get("changes"), **forecast_scope(kwargs))
    if missing:
        return {"sufficientData": False, "missingInputs": missing, "purchaseAmount": purchase_amount, "baseline": values,
                "reason": "月度结余不等于当前可用存款，不能据此判断买得起或买后生活困难。"}
    remaining = _round_money(starting_balance - purchase_amount)
    return {"sufficientData": True, "purchaseAmount": purchase_amount, "startingBalance": starting_balance,
            "cashAfterPurchase": remaining, "reserveAmount": reserve_amount,
            "reserveMargin": _round_money(remaining - reserve_amount) if reserve_amount is not None else None,
            "baseline": values, "missingInputs": ["reserveAmount"] if reserve_amount is None else [],
            "interpretation": "仅为现金支付后的余额测算；还需确认应急储备底线、近期必付账单及收入到账时间，不能直接给出确定的购买建议。"}


def runway(orders, *, as_of, starting_balance=None, changes=None, **kwargs):
    missing = (["startingBalance"] if starting_balance is None else []) + (["changes"] if not changes else [])
    values = baseline(orders, as_of, changes, **forecast_scope(kwargs))
    if missing or not values["sufficientData"]:
        return {"sufficientData": False, "missingInputs": missing, "baseline": values,
                "reason": "需要当前可动用余额和收入减少的具体幅度，不能用每月结余代替资产。"}
    deficit = max(0, -values["net"])
    return {"sufficientData": True, "startingBalance": starting_balance, "monthlyDeficit": deficit,
            "runwayMonths": _round_money(max(0, starting_balance) / deficit) if deficit else None,
            "baseline": values, "interpretation": "只在所给收入变化和支出不变的条件下测算；没有月度缺口不等于资金永远足够。"}


def data_quality(orders, *, as_of, **kwargs):
    flagged = [row for row in orders if float(row["money"]) > 0 and _nonrecurring_income(row)]
    anomalies = _anomalies(orders)
    return {"sufficientData": True, "nonrecurringIncomeCount": len(flagged),
            "nonrecurringIncomeTotal": _round_money(sum(float(row["money"]) for row in flagged)),
            "nonrecurringRemarks": list(dict.fromkeys(str(row.get("orderRemark") or row.get("costType")) for row in flagged))[:10],
            "missingPaymentMethodCount": sum(not row.get("bankName") for row in orders),
            "missingCategoryCount": sum(not row.get("costType") for row in orders),
            "possibleDuplicates": anomalies["possibleDuplicates"], "insufficientCategories": anomalies["insufficientCategories"],
            "interpretation": "退款、内部转入、出售等入账是需要核查口径的线索，不一定是记账错误；其金额不直接作为可持续收入预测。未发现重复不能证明无错误或账本完整。"}


DECISION_IMPLEMENTATIONS = {
    "projection": projection, "income_stability": income_stability, "optimization": optimization,
    "purchase": purchase, "runway": runway, "data_quality": data_quality,
}
