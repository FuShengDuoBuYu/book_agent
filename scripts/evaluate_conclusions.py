"""Evaluate report synthesis on fictional snapshots, not production users or history.

Automated checks screen delivery/number constraints; semanticGold requires a separate
full-answer review. Run with --output in the module's evaluations directory.
"""
import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from app.agent.artifacts import build_artifacts
from app.agent.evidence_report import EvidenceReporter
from app.core.model_usage import capture_usage
from scripts.conclusion_cases import conclusion_cases


async def evaluate(args):
    total = 0.0
    for case in conclusion_cases(datetime.now(ZoneInfo('Asia/Shanghai')).date()):
        if args.case and case['case'] not in args.case:
            continue
        if total >= args.max_cost_usd:
            raise RuntimeError('Known estimated evaluation budget exhausted')
        started = perf_counter()
        artifacts = build_artifacts(case['results'], case['output'], window=case['scope']['historyWindow'])
        with capture_usage() as usage:
            reply, validation = await asyncio.wait_for(EvidenceReporter().answer(
                case['question'], case['context'], case['results'], artifacts=artifacts,
                understanding={'requestedAnswers': [case['question']], 'evidenceNeeds': [r['tool'] for r in case['results'] if not r.get('supplemental')]}, analysis_scope=case['scope']), 120)
        record = {key: case[key] for key in ('case', 'question', 'semanticGold', 'expectedValue')}
        record.update(synthetic=True, sourceRevision=args.revision, reply=reply, characters=len(reply),
                      validation=validation, artifacts=artifacts, usage=usage.snapshot(),
                      elapsedSeconds=round(perf_counter()-started, 2), semanticReview='pending')
        checks = {'answer': bool(reply), 'plannedComposition': validation['compositionStatus'] == 'planned',
                  'noMarkdownData': '| ---' not in reply, 'normalLength': len(reply) <= (3200 if case['case']=='detailed_method' else 1200)}
        if case['expectedValue'] is not None:
            value = case['expectedValue']
            checks['expectedValueVisible'] = f'{value:g}' in reply.replace(',', '')
        if case['case']=='table':
            checks['requestedTable'] = any(a.get('presentation')=='requested' and a['rows'] for a in artifacts)
        record['structuralChecks'] = checks
        with args.output.open('a',encoding='utf-8') as out:
            out.write(json.dumps(record,ensure_ascii=False,default=str)+'\n')
        print(json.dumps({'case':case['case'],'characters':len(reply),'checks':checks,
                          'cost':record['usage']['estimatedCostUsd']},ensure_ascii=False),flush=True)
        cost = record['usage']['estimatedCostUsd']
        if cost is None:
            raise RuntimeError('Unknown model cost; stop this bounded run for review')
        total += cost
    print(json.dumps({'knownEstimatedCostUsd':round(total,8),'semanticReview':'required'}),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--case',action='append')
    parser.add_argument('--revision',required=True)
    parser.add_argument('--max-cost-usd',type=float,default=1.5)
    args=parser.parse_args()
    if args.output.exists():
        raise SystemExit('Choose a new run output; do not overwrite evaluation evidence')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    asyncio.run(evaluate(args))
