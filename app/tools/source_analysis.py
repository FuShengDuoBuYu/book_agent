"""Member-attributed source evidence. Labels and remarks are untrusted ledger text."""

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from statistics import median


CATALOG_LIMIT = 60
REMARK_LIMIT = 4
_CENT = Decimal("0.01")
_DATE_OR_AMOUNT = re.compile(
    r"(?<!\d)(?:\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?|月)?|"
    r"\d{1,2}月(?:\d{1,2}日)?|\d{1,2}日|\d+(?:\.\d+)?\s*元)(?!\d)"
)
_GENERIC_REMARKS = {"无", "无备注", "未填写", "收入", "支出", "收款", "付款", "其他", "其它", "未分类"}


def money(value) -> float:
    return float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP))


def nonrecurring_income(order: dict) -> bool:
    text = f"{order.get('costType', '')} {order.get('orderRemark', '')}"
    if any(word in text for word in ("退款", "退货", "报销", "借款", "借入", "归还借款", "出售", "卖车", "旧车", "二手", "一次性", "年终奖")):
        return True
    earned = ("工资", "薪", "家教", "课时", "授课", "劳务", "兼职", "稿费", "租金", "助研", "助教", "补贴")
    if any(word in text for word in earned):
        return False
    return any(word in text for word in ("转入", "转账", "存款", "借还", "红包", "礼金", "报账"))


def _observed_label(remark: str, category: str) -> str:
    label = _DATE_OR_AMOUNT.sub(" ", unicodedata.normalize("NFKC", remark))
    label = " ".join(label.split()).strip(" -_/，,。")
    if not label or label in _GENERIC_REMARKS or not any(char.isalpha() for char in label):
        return category
    return label


def _key(*fields) -> str:
    packed = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
    return "src_" + hashlib.sha256(packed).hexdigest()[:24]


def _identity(order: dict) -> tuple[str, str, str, str, bool]:
    member = order.get("memberKey") or "member_unknown"
    category = str(order.get("costType") or "").strip() or "未分类"
    source = _observed_label(str(order.get("orderRemark") or ""), category)
    metric = "income" if float(order["money"]) > 0 else "expense"
    return member, category, source, metric, metric == "income" and nonrecurring_income(order)


def observed_source_key(order: dict) -> str:
    """Stable key for a nonzero authorized ledger row, independent of query order."""
    return _key("observed", *_identity(order))


def validate_source_groups(source_groups, known_keys: set[str]) -> dict[str, tuple[str, str]]:
    """Return observed key -> (alias label, group key). Reject an invalid plan atomically."""
    if source_groups is None:
        return {}
    if not isinstance(source_groups, list) or len(source_groups) > CATALOG_LIMIT:
        raise ValueError("来源分组格式或数量不正确")
    aliases = {}
    for group in source_groups:
        if not isinstance(group, dict):
            raise ValueError("来源分组必须是对象")
        label, keys = group.get("label"), group.get("sourceKeys")
        if not isinstance(label, str) or not 0 < len(label.strip()) <= 80:
            raise ValueError("来源分组缺少有效名称")
        if not isinstance(keys, list) or not 0 < len(keys) <= CATALOG_LIMIT:
            raise ValueError("来源分组缺少有效键列表")
        if any(not isinstance(key, str) or key not in known_keys or key in aliases for key in keys):
            raise ValueError("来源分组含未知或重复键")
        if len(set(keys)) != len(keys):
            raise ValueError("来源分组含重复键")
        token = _key("group", sorted(keys))
        aliases.update((key, (label.strip(), token)) for key in keys)
    return aliases


def source_aliases(orders: list[dict], source_groups) -> dict:
    if source_groups is None or source_groups == []:
        return {}
    known_keys = {observed_source_key(order) for order in orders if float(order["money"]) != 0}
    return validate_source_groups(source_groups, known_keys)


def source_rows(orders: list[dict], aliases: dict) -> list[dict]:
    groups = {}
    # The earliest distinct remarks are reproducible across overlapping queries and DB sort orders.
    def ledger_date(order):
        return date(int(order["year"]), int(order["month"]), int(order["day"]))

    for order in sorted(orders, key=lambda r: (ledger_date(r), str(r.get("orderRemark") or ""))):
        amount = abs(Decimal(str(order["money"])))
        if not amount:
            continue
        member, category, label, metric, excluded = _identity(order)
        observed_key = _key("observed", member, category, label, metric, excluded)
        key = observed_key
        if observed_key in aliases:
            label, token = aliases[observed_key]
            key = _key("alias", token, member, metric, excluded)
        if key not in groups:
            groups[key] = {"sourceKey": key, "source": label[:120], "memberKey": member,
                           "member": order.get("memberName") or "未知成员", "metric": metric,
                           "excluded": excluded, "categories": set(), "observedSourceKeys": set(),
                           "remarks": [], "monthlyAmounts": defaultdict(Decimal), "recordCount": 0, "dates": set()}
        row = groups[key]
        row["dates"].add(ledger_date(order))
        row["categories"].add(category)
        row["observedSourceKeys"].add(observed_key)
        row["monthlyAmounts"][f"{int(order['year']):04}-{int(order['month']):02}"] += amount
        row["recordCount"] += 1
        remark = str(order.get("orderRemark") or "").strip()[:160]
        if remark and len(row["remarks"]) < REMARK_LIMIT and remark not in row["remarks"]:
            row["remarks"].append(remark)
    for row in groups.values():
        dates = sorted(row.pop("dates"))
        gaps = [(end - start).days for start, end in zip(dates, dates[1:])]
        row["medianGapDays"] = median(gaps) if gaps else None
        row["categories"] = sorted(row["categories"])
        row["category"] = "、".join(row["categories"])
        row["observedSourceKeys"] = sorted(row["observedSourceKeys"])
        row["monthlyAmounts"] = {month: money(amount) for month, amount in row["monthlyAmounts"].items()}
        row["amount"] = money(sum(Decimal(str(value)) for value in row["monthlyAmounts"].values()))
    return sorted(groups.values(), key=lambda r: (-r["amount"], r["sourceKey"]))


def source_catalog(orders: list[dict], *, source_groups=None, **kwargs) -> dict:
    rows = source_rows(orders, source_aliases(orders, source_groups))
    return {"sources": rows[:CATALOG_LIMIT], "sourceCount": len(rows),
            "omittedSourceCount": max(0, len(rows) - CATALOG_LIMIT),
            "excludedIncomeTotal": money(sum(r["amount"] for r in rows if r["excluded"])),
            "coverageNote": "目录按累计金额排序；超出展示上限的来源仍计入预测。备注仅为原始账单描述，来源实际含义可能需要确认。"}


def income_source_forecasts(rows: list[dict], observed: list[str], baseline: list[str]) -> list[dict]:
    forecasts = []
    for row in rows:
        if row["metric"] != "income":
            continue
        recorded = row["monthlyAmounts"]
        amounts = {month: recorded.get(month, 0.0) for month in observed}
        values = [amounts[month] for month in baseline]
        active = [month for month in baseline if month in recorded]
        if row["excluded"]:
            status = "excluded"
        elif not active:
            status = "inactive"
        elif len(recorded) == 1 and active == baseline[-1:]:
            status = "new"
        elif len(active) < len(baseline) or len(active) < 2:
            status = "sparse"
        else:
            status = "observed"
        estimate = lower = upper = None
        if len(baseline) >= 2:
            estimate, lower, upper = (0.0, 0.0, 0.0) if row["excluded"] else (
                money(median([Decimal(str(v)) for v in values])), min(values), max(values))
        forecasts.append({**row, "monthlyAmounts": amounts, "recordedMonths": list(recorded),
                          "status": status, "estimate": estimate, "lower": lower, "upper": upper})
    return forecasts
