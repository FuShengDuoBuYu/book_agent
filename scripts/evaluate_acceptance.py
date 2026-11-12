"""User-approved analysis questions on a controlled synthetic ledger.

The JSONL contains synthetic answers, plans and evidence, never production records.
Machine checks are necessary checks; explanations also require qualitative review.
"""

import argparse
import asyncio
import json
from time import perf_counter

from app.agent.book_agent import BookAgent
from scripts.evaluate_analysis import FixtureLedger, FixtureMemory


QUESTIONS = [
    ("diagnosis", "综合看看我最近半年的财务情况，最值得注意的三个问题是什么？"),
    ("drivers", "我最近感觉花钱越来越多，到底是哪里出了问题？"),
    ("resilience", "我的收入稳定吗？如果主要收入来源减少，我能承受多久？"),
    ("outlook", "按现在的趋势，未来三个月我的收支会怎样？最大的变数是什么？"),
    ("optimize", "我想每月多留下1000元，但不想影响正常生活，应该怎么调整？"),
    ("purchase", "我下个月想花6000元买电脑，现在买合适吗？会影响后面的生活吗？"),
    ("goal", "假设现在有10000元，想三个月存到20000元，照目前的收支能做到吗？做不到该改哪里？"),
    ("quality", "账本里有没有不合理或容易误判的地方，会不会影响你对我的分析？"),
]
FOLLOWUPS = [
    ("chain_1", "未来三个月，我大概能存下多少钱？"),
    ("chain_2", "家教可能会少一半，重新算一下。"),
    ("chain_3", "房租不能动，吃饭也不想再省，其他地方还能怎么调整？"),
    ("chain_4", "那把目标推迟两个月，压力能小多少？"),
]
VARIANTS = [
    ('variant_source', '家教收入减少30%，其余不变，未来三个月能新增结余多少？'),
    ('variant_purchase', '我现在可动用12000元，买6000元电脑后还要留5000元应急钱，是否足够？'),
    ('variant_goal', '现在有10000元，保持当前收支，多久能存到20000元？'),
    ('variant_runway', '假设所有收入都停了，现有可动用10000元，支出维持原样，能撑多久？'),
]


def assess(record):
    """Necessary automated checks; human review must still assess usefulness."""
    metadata = record.get('metadata', {})
    results = {row['tool']: row['evidence'] for row in metadata.get('evidence', {}).get('analysisResults', [])}
    name = record['case']
    checks = {'answered': bool(record.get('reply')) and not record.get('error'),
              'interpretationReviewed': metadata.get('validation', {}).get('status') == 'reviewed'}
    def has(tool, key, expected):
        return results.get(tool, {}).get(key) == expected
    if name == 'diagnosis':
        checks['completeMonthlyAverage'] = results.get('cashflow', {}).get('completeMonthAverage', {}).get('net') == 3850
        checks['changeExplained'] = has('changes', 'expenseChange', 900)
    if name == 'drivers':
        checks['changeExplained'] = has('changes', 'expenseChange', 900)
    if name == 'resilience':
        checks['incomeConcentration'] = bool(results.get('income_stability', {}).get('sources'))
        checks['unknownRunway'] = 'startingBalance' in results.get('runway', {}).get('missingInputs', [])
    if name in {'outlook', 'chain_1', 'chain_2', 'chain_4'}:
        total = {'outlook': 12000, 'chain_1': 12000, 'chain_2': 9600, 'chain_4': 16000}[name]
        checks['projectionArithmetic'] = has('projection', 'totalNet', total)
        checks['answerUsesProjection'] = str(total) in record.get('reply', '').replace(',', '')
    if name == 'optimize':
        checks['targetNotInvented'] = has('optimization', 'monthlyImprovement', 1000)
        checks['essentialsProtected'] = has('optimization', 'reviewableMonthlySpend', 0)
    if name == 'purchase':
        checks['unknownAffordability'] = 'startingBalance' in results.get('purchase', {}).get('missingInputs', [])
    if name == 'goal':
        checks['conditionalBalance'] = has('savings', 'projectedBalance', 22000)
    if name == 'quality':
        checks['qualityAndSampleLimits'] = bool(results.get('data_quality', {}).get('insufficientCategories'))
    if name in {'chain_3', 'chain_4'}:
        context = metadata.get('analysisContext', {})
        checks['protectedRetained'] = {'房租', '餐饮'} <= set(context.get('protectedCategories', []))
        checks['sourceScenarioRetained'] = any(c.get('source') == '家教' and c.get('percent') == -50 for c in context.get('changes', []))
    if name == 'variant_source':
        checks['sourceSpecificArithmetic'] = has('projection', 'totalNet', 10560)
        checks['answerUsesProjection'] = '10560' in record.get('reply', '').replace(',', '')
    if name == 'variant_purchase':
        checks['cashAfterPurchase'] = has('purchase', 'cashAfterPurchase', 6000)
        checks['reserveMargin'] = has('purchase', 'reserveMargin', 1000)
    if name == 'variant_goal':
        checks['timeToGoal'] = has('savings', 'estimatedMonthsToTarget', 3)
        checks['answerUsesTimeToGoal'] = '3 个月' in record.get('reply', '')
    if name == 'variant_runway':
        checks['allIncomeStops'] = has('runway', 'monthlyDeficit', 2600)
        checks['runwayUsesCash'] = has('runway', 'runwayMonths', 3.85)
    return checks


def make_agent():
    agent = BookAgent(FixtureLedger())
    agent.memory = FixtureMemory()
    return agent


async def run_case(agent, name, question, timeout):
    started = perf_counter()
    record = {"case": name, "question": question, "synthetic": True}
    try:
        reply, _ = await asyncio.wait_for(agent.chat("synthetic-user", question), timeout=timeout)
        record.update(reply=reply, metadata=agent.memory.messages[-1].get("metadata", {}))
    except Exception as exc:
        record.update(reply="", error=type(exc).__name__)
    record["elapsedSeconds"] = round(perf_counter() - started, 2)
    record['checks'] = assess(record)
    record['automatedPass'] = all(record['checks'].values())
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


async def main(args):
    for name, question in QUESTIONS:
        if not args.case or name in args.case:
            await run_case(make_agent(), name, question, args.timeout)
    if not args.case or "chain" in args.case:
        agent = make_agent()
        for name, question in FOLLOWUPS:
            await run_case(agent, name, question, args.timeout)
    for name, question in VARIANTS:
        if not args.case or 'variants' in args.case or name in args.case:
            await run_case(make_agent(), name, question, args.timeout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append")
    parser.add_argument("--timeout", type=int, default=240)
    asyncio.run(main(parser.parse_args()))
