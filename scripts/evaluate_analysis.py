"""Exercise the configured model with synthetic ledgers and conversation memory.

Run from the repository root with ``python -m scripts.evaluate_analysis``.
No database or user ledger is accessed. Output can be retained for model/prompt comparisons.
"""

import argparse
import asyncio
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent.book_agent import BookAgent
from app.tools.financial_analysis import shift_month


class FixtureLedger:
    def __init__(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        self.orders = []
        for index in range(-6, 1):
            month = shift_month(today, index)
            for amount, category, remark, day in (
                (5000, "其他", "固定工资", 2), (1600, "收入", "家教课时费", 3),
                (-1800, "房租", "月租", 4), (-600, "餐饮", "日常餐饮", 5),
                (-200, "交通", "公交", 6),
            ):
                self.orders.append({"id": len(self.orders) + 1, "year": month.year, "month": month.month,
                                    "day": day, "money": amount, "costType": category, "orderRemark": remark})
        last = shift_month(today, -1)
        self.orders.append({"id": 100, "year": last.year, "month": last.month, "day": 8,
                            "money": -900, "costType": "医疗", "orderRemark": "一次就诊"})

    async def search_orders(self, request, **kwargs):
        records = [row for row in self.orders
                   if (request.if_ignore_year or row["year"] == request.year)
                   and (request.if_ignore_month or row["month"] == request.month)
                   and (request.if_ignore_day or row["day"] == request.day)
                   and (request.search_cost_type == "不限" or row["costType"] == request.search_cost_type)
                   and (not request.search_order_remark or request.search_order_remark in row["orderRemark"])]
        return {"success": True, "message": "synthetic fixture", "data": {"userInfo": [{"fixture": True}], "ordersInfo": records}}


class FixtureMemory:
    def __init__(self):
        self.messages = []

    async def ensure_session(self, **kwargs):
        return "synthetic-conversation"

    async def get_recent_messages(self, **kwargs):
        return self.messages[-6:]

    async def add_message(self, **kwargs):
        self.messages.append(kwargs)


CASES = [
    ("overview", "全面分析我最近半年的收支结构、变化原因和固定开销，有什么值得注意？", {"cashflow", "trends", "recurring"}),
    ("changes", "最近几个月为什么开销变多了？具体哪些项目在推动变化？", {"changes"}),
    ("anomalies", "帮我查一下最近半年有没有异常开销、疑似重复账单。", {"anomalies"}),
    ("forecast", "根据历史规律预估下个月收入、支出和结余，说明依据和不确定性。", {"forecast"}),
    ("scenario", "如果下个月家教增加8节课，每节200元，并减少300元餐饮支出，结余会怎样？", {"scenario"}),
    ("budget", "本月预算4000元，已花多少，还剩多少，接下来每天还能花多少？", {"budget"}),
    ("savings", "现在已有存款10000元，想在3个月内存到20000元，以我目前收支能实现吗？", {"savings"}),
]


async def evaluate(selected=None):
    passed = True
    for name, question, expected_tools in CASES:
        if selected and name not in selected:
            continue
        agent = BookAgent(FixtureLedger())
        agent.memory = FixtureMemory()
        reply, _ = await asyncio.wait_for(agent.chat("synthetic-user", question), timeout=240)
        metadata = agent.memory.messages[-1].get("metadata", {})
        planned = {item["tool"] for item in metadata.get("plan", {}).get("analyses", [])}
        checks = {"expectedTools": expected_tools <= planned, "nonemptyAnswer": bool(reply)}
        evidence = metadata.get("evidence", {}).get("analysisResults", [])
        results = {row["tool"]: row["evidence"] for row in evidence}
        reply_numbers = {float(value.replace(",", "")) for value in re.findall(r"\d[\d,]*(?:\.\d+)?", reply)}
        if name == "overview":
            checks["fullHistory"] = results.get("cashflow", {}).get("incomeTotal", 0) >= 39600
        if name == "changes":
            checks["expenseDriver"] = results.get("changes", {}).get("expenseChange") == 900
            checks["answerMentionsDriverAmount"] = 900 in reply_numbers
        if name == "forecast":
            estimates = {row["evidence"].get("metric"): row["evidence"].get("estimate")
                         for row in evidence if row["tool"] == "forecast"}
            checks["allThreeForecasts"] = estimates == {"income": 6600, "expense": 2600, "net": 4000}
            checks["answerUsesEstimates"] = {6600, 2600, 4000} <= reply_numbers
            target = shift_month(datetime.now(ZoneInfo("Asia/Shanghai")).date(), 1).strftime("%Y-%m")
            checks["targetMonth"] = all(row["evidence"].get("targetMonth") == target for row in evidence if row["tool"] == "forecast")
        if name == "scenario":
            checks["scenarioArithmetic"] = results.get("scenario", {}).get("net") == 5900
            checks["answerUsesScenario"] = 5900 in reply_numbers
        if name == "budget":
            checks["budgetArithmetic"] = results.get("budget", {}).get("remaining") == 1400
            checks["answerUsesBudget"] = 1400 in reply_numbers
        if name == "savings":
            checks["savingsArithmetic"] = results.get("savings", {}).get("projectedBalance") == 22000
            checks["answerUsesProjection"] = 22000 in reply_numbers
        success = all(checks.values())
        passed = passed and success
        print(json.dumps({"case": name, "synthetic": True, "passed": success,
                          "tools": sorted(planned), "checks": checks, "durationMs": metadata.get("durationMs"),
                          "plan": metadata.get("plan"),
                          "reply": reply}, ensure_ascii=False), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", dest="cases")
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(arguments.cases)))
