"""Run multi-turn intent acceptance against a configured model with a fictional ledger.

From the repository root: python -m scripts.evaluate_intent --case forecast_followups
One JSONL record is printed per turn. --output FILE also appends the records to disk.
"""

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from langsmith import tracing_context

from app.agent.book_agent import BookAgent
from app.tools.financial_analysis import shift_month
from scripts.evaluate_analysis import FixtureLedger, FixtureMemory


OWNER = "synthetic-owner-amber"
PARTNER = "synthetic-owner-indigo"
FAMILY = "synthetic-household"
STAGES = ("星港海报初排", "星港海报调版", "星港海报完稿")


class IntentLedger(FixtureLedger):
    """The same visible data and ownership filters apply to every turn in a case."""

    def __init__(self, today):
        self.orders = []
        self.lookups = []
        self.profiles = [{"phoneNum": OWNER, "nickname": "晴禾"}, {"phoneNum": PARTNER, "nickname": "宇川"}]
        for index in range(6):
            month = shift_month(today, index - 6)
            records = [
                (OWNER, 3200, "收入", "河畔编辑部月结"),
                (OWNER, 500, "项目收入", STAGES[index % 3]),
                (OWNER, -1100, "房租", "公寓租金"),
                (OWNER, -180, "交通", "通勤月票"),
                (OWNER, -220, "餐饮", "日常午餐"),
                (OWNER, -180, "展览", "展厅票券"),
                (PARTNER, 2100, "收入", "河畔编辑部月结"),
                (PARTNER, 450, "劳务", "种苗配送"),
                (PARTNER, -370, "日用品", "花架配件"),
            ]
            if index == 5:
                records.extend([(OWNER, 8000, "其他", "旧设备一次性出售"),
                                (OWNER, -260, "展览", "春展加场票券")])
            for owner, amount, category, remark in records:
                self.orders.append({"id": len(self.orders) + 1, "year": month.year, "month": month.month,
                                    "day": 5, "userId": owner, "money": amount,
                                    "costType": category, "orderRemark": remark})

    async def search_orders(self, request, *, family_scope=""):
        result = await super().search_orders(request)
        family = request.mode == "家庭版"
        if family and family_scope != FAMILY:
            raise AssertionError("Synthetic family lookup lacks its authorized scope")
        data = result["data"]
        data["userInfo"] = self.profiles if family else self.profiles[:1]
        if not family:
            data["ordersInfo"] = [r for r in data["ordersInfo"] if r["userId"] == OWNER]
        self.lookups.append({"mode": request.mode, "matches": len(data["ordersInfo"])})
        return result


def dialogues(today):
    target = shift_month(today, 2)
    return {
        "forecast_followups": ("个人版", [
            ("forecast", f"按我的个人账本，预计{target.year}年{target.month}月能进账多少？同一个项目的不同工序算一个收入来源。"),
            ("table", "刚才这个数拆开，每个项目各预测多少？做成表，合计也列上。"),
            ("notes", "你有没有看过备注？请现在查一下，列出你把哪些原始备注认作同一个项目。"),
            ("method", "那刚才预测的那个月，到底拿哪几个月作参照？具体怎么从逐个项目算到总数，一次性的款项怎么处理？"),
        ]),
        "family_sources": ("家庭版", [
            ("family", "在我们家上一个完整月的收入里，谁从哪些地方收到了钱？即使备注相同，也请按人分清楚。"),
        ]),
        "expense_decision": ("个人版", [
            ("change", "对照我最近两个完整月，哪项开销把总支出拉高了，增加了多少？"),
            ("saving", "沿用刚才这两个完整月的开销作参照，房租和交通继续保留。我想每月多留三百，哪些地方可调整，还差多少？"),
            ("buy", "按刚才讲的情况，我现在能直接拿七百元买扫描仪吗？还需要什么信息才能判断？"),
        ]),
        "cross_dimensions": ("个人版", [
            ("aggregate", "对照上上个月和上个月，排除房租、交通后，我的支出按月份和类别列出金额、账单笔数和每笔均额，各类别增加多少？给表，不用画图。"),
            ("weekday", "沿用这两个月的范围，只看展览，按星期几分组，哪天的已记支出最多？给金额和笔数。"),
        ]),
    }


def visible_text(reply, artifacts):
    # Only fields rendered to the user count as communicating an answer.
    return reply + "\n" + "\n".join(
        " ".join(map(str, [a.get("title", ""), *a.get("notes", []),
                           *(value for row in a.get("rows", []) for value in row.values())]))
        for a in artifacts)


def amounts_in(text):
    return {float(match.replace(",", "")) for match in re.findall(r"(?<![\d,])\d+(?:,\d{3})*(?:\.\d+)?", text)}


def month_visible(month, text):
    year, number = month.split("-")
    return month in text or bool(re.search(rf"{year}\s*[年/.]\s*0?{int(number)}(?:月|\b)", text))


def check_turn(kind, today, reply, metadata, lookups):
    analyses = metadata.get("evidence", {}).get("analysisResults", [])
    tools = {a["tool"] for a in analyses}
    artifacts = metadata.get("artifacts", [])
    visible = visible_text(reply, artifacts)
    amounts = amounts_in(visible)
    evidence = {a["tool"]: a["evidence"] for a in analyses}
    forecast = next((a["evidence"] for a in analyses if a["tool"] == "forecast"
                     and a["evidence"].get("metric") == "income"), {})
    target = shift_month(today, 2).strftime("%Y-%m")
    checks = {"answered": bool(reply.strip()),
              "explanationReviewed": metadata.get('validation', {}).get('status') == 'reviewed',
              "noPrivateIdentifiers": all(
        value not in visible for value in (OWNER, PARTNER, FAMILY))}
    if kind in {"forecast", "table", "method"}:
        components = [r for r in forecast.get("sourceForecasts", []) if not r.get("excluded")]
        charges = [r.get("estimate") for r in components]
        checks.update({"targetPreserved": forecast.get("targetMonth") == target,
                       "sourceBaseline": forecast.get("estimate") == 3700 and sorted(v for v in charges if v) == [500, 3200],
                       "reconciled": bool(charges) and None not in charges and sum(charges) == forecast.get("estimate"),
                       "totalCommunicated": 3700 in amounts})
    if kind == "table":
        checks["componentTable"] = any(a.get("kind") == "table" and
            {row.get("estimate") for row in a.get("rows", [])} >= {500, 3200, 3700} for a in artifacts)
    if kind == "notes":
        remark_rows = [r for a in analyses for key in ("sourceForecasts", "sources")
                       if isinstance(a["evidence"].get(key), list) for r in a["evidence"][key]]
        remarks = {remark for row in remark_rows for remark in row.get("remarks", [])}
        checks.update({"queriedLedger": any(entry["matches"] for entry in lookups),
                       "sourceOrForecastExecuted": bool({"sources", "forecast"} & tools),
                       "stagesAvailable": set(STAGES) <= remarks,
                       "observedRemarksCommunicated": all(word in visible for word in STAGES)})
    if kind == "method":
        expected = [shift_month(today, offset).strftime("%Y-%m") for offset in (-3, -2, -1)]
        checks.update({"referenceMonths": forecast.get("baselineMonths") == expected,
                       "referenceMonthsCommunicated": all(month_visible(month, visible) for month in expected),
                       "exclusionEvidence": forecast.get("excludedIncomeTotal") == 8000,
                       "methodCommunicated": bool(re.search(r"中位|排序|居中", visible))
                            and bool(re.search(r"一次性|旧设备", visible))})
    if kind == "family":
        last = shift_month(today, -1).strftime("%Y-%m")
        rows = evidence.get("sources", {}).get("sources", [])
        amounts_by_owner = {r["member"]: r["monthlyAmounts"].get(last) for r in rows
                            if r.get("metric") == "income" and "河畔编辑部月结" in r.get("remarks", [])}
        checks.update({"familyOnly": bool(lookups) and all(entry["mode"] == "家庭版" for entry in lookups)
                           and all(a["mode"] == "家庭版" for a in analyses),
                       "memberAttribution": amounts_by_owner == {"晴禾": 3200, "宇川": 2100},
                       "incomeWindow": bool(rows) and all(set(r["monthlyAmounts"]) <= {last} for r in rows),
                       "distinctOwnersCommunicated": all(label in visible for label in ("晴禾", "宇川", "河畔", "种苗"))
                           and {3200, 2100, 450} <= amounts})
    if kind == "change":
        changes = evidence.get("changes", {})
        checks.update({"driverAmount": changes.get("expenseChange") == 260 and any(
            row["name"] == "展览" and row["change"] == 260 for row in changes.get("expenseDrivers", [])),
            "driverCommunicated": "展览" in visible and 260 in amounts})
    if kind == "saving":
        optimization = evidence.get("optimization", {})
        checks.update({"protectedCosts": {"房租", "交通"} <= set(optimization.get("protectedCategories", [])),
                       # The explicitly retained two-month sample has 180 and 440
                       # in exhibition expenses; its median is 310, not the
                       # six-month baseline of 180 used by an earlier assertion.
                       "targetGap": optimization.get("reviewableMonthlySpend") == 310
                           and optimization.get("monthlyImprovement") == 300
                           and optimization.get("gapBeyondReviewableSpend") == 0,
                       "candidateCommunicated": "展览" in visible and 310 in amounts})
    if kind == "aggregate":
        aggregate = evidence.get('aggregate', {})
        rows = aggregate.get('rows', [])
        categories = {r.get('category') for r in rows}
        changes = aggregate.get('comparisons', [])
        # A month grouping can also compare the displayed per-month values.
        months = [shift_month(today, offset).strftime('%Y-%m') for offset in (-2, -1)]
        exhibit = {r.get('month'): r for r in rows if r.get('category') == '展览'}
        checked = any(r.get('category') == '展览' and r.get('change') == 260 for r in changes)
        checked = checked or (exhibit.get(months[0], {}).get('expense') == 180
                              and exhibit.get(months[1], {}).get('expense') == 440)
        checks.update(filteredTotal=aggregate.get('totals', {}).get('expense') == 1060,
                      categories=categories == {'餐饮', '展览'},
                      perRecordStatistics=any(r.get('category') == '展览' and r.get('count') == 2
                          and r.get('meanTransactionAmount') == 220 for r in rows),
                      comparisonAvailable=checked,
                      tableRendered=any(a.get('kind') == 'table' for a in artifacts),
                      interpretedChange='展览' in visible and 260 in amounts)
    if kind == "weekday":
        aggregate = evidence.get('aggregate', {})
        rows = aggregate.get('rows', [])
        top = next((r for r in rows if r.get('weekday') == '星期一'), {})
        checks.update(onlyExhibition=aggregate.get('totals', {}).get('expense') == 620,
                      weekdayStatistic=top.get('expense') == 440 and top.get('count') == 2,
                      answerUsesWeekday='星期一' in visible and 440 in amounts)
    if kind == "buy":
        purchase = evidence.get("purchase", {})
        checks.update({"purchaseValue": purchase.get("purchaseAmount") == 700,
                       "balanceMissing": "startingBalance" in purchase.get("missingInputs", [])
                           and not purchase.get("sufficientData"),
                       "balanceGapCommunicated": bool(re.search(r"余额|存款|可用现金|可动用", visible))})
    return checks


async def evaluate(selected, output_file, timeout):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    started = datetime.now(UTC).isoformat()
    passed = True
    for name, (mode, turns) in dialogues(today).items():
        if selected and name not in selected:
            continue
        ledger = IntentLedger(today)
        agent = BookAgent(ledger)
        agent.memory = FixtureMemory()
        session = None
        for index, (kind, question) in enumerate(turns, 1):
            position = len(ledger.lookups)
            start = perf_counter()
            record = {"run": started, "synthetic": True, "case": name, "turn": index, "purpose": kind,
                      "mode": mode, "question": question}
            try:
                reply, session = await asyncio.wait_for(agent.chat(
                    OWNER, question, session_id=session, mode=mode, family_id=FAMILY if mode == "家庭版" else None),
                    timeout=timeout)
                message = agent.memory.messages[-1]
                metadata = message.get("metadata", {}) if message.get("role") == "assistant" else {}
                lookups = ledger.lookups[position:]
                checks = check_turn(kind, today, reply, metadata, lookups)
                record.update({"checks": checks, "passed": all(checks.values()), "reply": reply,
                               "executedTools": [row["tool"] for row in metadata.get("evidence", {}).get("analysisResults", [])],
                               "queries": lookups, "understanding": metadata.get("plan", {}).get("understanding"),
                               "investigationStatus": (metadata.get("investigation") or {}).get("status"),
                               "artifactKinds": [a.get("kind") for a in metadata.get("artifacts", [])],
                               "analysisVersion": metadata.get("analysisVersion"), "model": metadata.get("model")})
                record.update(plan=metadata.get('plan'), investigation=metadata.get('investigation'),
                              analysisContext=metadata.get('analysisContext'),
                              planningReview=metadata.get('planningReview'),
                              validation=metadata.get('validation'), planningIssues=metadata.get('planningIssues'))
            except Exception as exc:
                record.update({"passed": False, "error": type(exc).__name__})
            record["durationMs"] = round((perf_counter() - start) * 1000)
            line = json.dumps(record, ensure_ascii=False)
            print(line, flush=True)
            if output_file:
                with output_file.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            passed = passed and record["passed"]
            if "error" in record:
                break
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=list(dialogues(datetime.now().date())), dest="cases")
    parser.add_argument("--output", type=Path, help="Also append JSONL to this file.")
    parser.add_argument("--timeout", type=float, default=240, help="Maximum seconds per chat turn.")
    args = parser.parse_args()
    with tracing_context(enabled=False):
        raise SystemExit(asyncio.run(evaluate(args.cases, args.output, args.timeout)))
