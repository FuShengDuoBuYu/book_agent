"""Separate complete audit evidence, bounded composition input and fixed decisions."""

from dataclasses import dataclass
from itertools import zip_longest
import re

from app.agent.analysis_context import DECISION_TOOLS
from app.agent.report_context import _prompt_budget
from app.agent.report_values import literal, value_catalog


@dataclass
class ReportContext:
    facts: list
    selected: list
    values: list
    judgments: list
    payload: dict
    budget: dict
    char_limit: int
    findings: dict


def detail_requested(question):
    if re.search(r'只.{0,5}结论|一句话|简短|不要展开|别展开', question or ''):
        return False
    return bool(re.search(r'详细|深入|完整报告|逐项|展开(?:分析|说明|讲)', question or ''))


def _judgments(facts, findings):
    groups = {}
    for fact in facts:
        binding = fact.get('binding', {})
        if binding.get('primary') is not False and binding.get('tool') in DECISION_TOOLS:
            groups.setdefault(binding.get('resultGroup'), []).append(fact)
    result = []
    for group, rows in groups.items():
        finding = findings.get(group, {})
        binding = rows[0].get('binding', {})
        if finding.get('scope') != binding.get('scope') or finding.get('tool') != binding.get('tool'):
            continue
        tool, states = binding['tool'], finding.get('states', {})
        anchor = next((f for f in rows if f.get('binding', {}).get('headline')), rows[0])
        if states.get('sufficientData') is False:
            text = '目前还不能确定这项结果。' + anchor['text']
        elif tool == 'forecast':
            text = anchor['text'].split('，历史情景范围')[0].rstrip('。') + '。'
        elif tool == 'purchase':
            if states.get('coversPrice') is False:
                text = '按给定现金和价格，现有资金不足以支付这笔购买。'
            elif states.get('coversPriceAndReserve') is False:
                text = '虽然能支付买价，但买后达不到你给定的应急储备底线，这笔购买暂不满足你的条件。'
            elif states.get('coversPriceAndReserve') is True:
                text = '按给定现金、买价和储备底线，这笔购买在金额上可行，买后仍能保留所设储备。'
            else:
                text = '给定现金可以支付买价；尚未给出应急储备底线，不能据此确认买后是否影响生活。'
        elif tool == 'savings':
            state = states.get('meetsDeadline')
            text = ('按当前收支假设，期限内能达到目标。' if state is True else
                    '按当前收支假设，给定期限内仍不能达到目标。' if state is False else
                    next((s for s in finding.get('observations', []) if '目标' in s or '达标' in s), anchor['text']))
        elif tool == 'budget':
            text = anchor['text'].split('按含今天')[0]
        elif tool == 'optimization':
            text = next((s for s in finding.get('observations', []) if '缺口' in s or '目标' in s or '上界' in s), anchor['text'])
        elif tool == 'runway':
            text = next((s for s in finding.get('observations', []) if '当前情景' in s), anchor['text'])
        else:
            text = anchor['text'].split('。')[0] + '。'
        result.append({'id': f'j{len(result) + 1}', 'text': text, 'factIds': [anchor['id']],
                       'binding': dict(binding), 'states': dict(states)})
    if len({j['binding'].get('scope') for j in result}) > 1:
        for judgment in result:
            judgment['text'] = '【' + judgment['binding']['scope'] + '】' + judgment['text']
    return result


def _catalog(facts):
    by_id = {f['id']: f for f in facts}
    values = value_catalog(facts)
    for value in values:
        value['binding'] = dict(by_id[value['factId']].get('binding', {}))
        value['role'] = value['label']
    for fact in facts:
        known = {(v['value'], v['unit']) for v in values if v['factId'] == fact['id']}
        for calendar in fact.get('calendars', []):
            unit = 'date' if len(calendar) == 10 else 'month'
            if (calendar, unit) not in known:
                values.append({'id': f'v{len(values) + 1}', 'factId': fact['id'], 'value': calendar,
                               'unit': unit, 'label': '证据日期', 'role': '证据日期', 'display': calendar,
                               'binding': dict(fact.get('binding', {}))})
        # Numeric/markup-bearing names are inert ledger labels, never scalar money.
        binding = fact.get('binding', {})
        labels = [binding.get(k) for k in ('source', 'member', 'category')]
        labels += re.findall(r'`+\s+([^`\n]+?)\s+`+', fact['text'])
        for label in dict.fromkeys(x for x in labels if isinstance(x, str) and x):
            if re.search(r'\d|[{}<>]|https?://', label) and not re.fullmatch(r'\d{4}-\d{2}(?:-\d{2})?', label):
                values.append({'id': f'n{len(values) + 1}', 'factId': fact['id'], 'value': label,
                               'unit': 'label', 'label': '账单原文标签，不是金额', 'role': 'ledger_label',
                               'display': literal(label), 'binding': dict(binding)})
    return values


def _ordered(facts, judgments, priorities):
    pinned = {key for j in [*judgments, *priorities] for key in j['factIds']}
    mandatory = [f for f in facts if f['id'] in pinned]
    groups = {}
    for fact in facts:
        if fact['id'] not in pinned:
            groups.setdefault(fact.get('binding', {}).get('resultGroup', fact['id']), []).append(fact)
    # Preserve diversity instead of filling the entire budget with one catalogue.
    optional = [f for batch in zip_longest(*groups.values()) for f in batch if f is not None]
    return mandatory, optional


def audit_report_context(payload):
    facts = payload.get('facts', [])
    return ReportContext(facts, [], _catalog(facts), _judgments(facts, payload.get('analysisFindings', {})),
                         {k: payload[k] for k in ('question', 'reviewPriorities') if k in payload}, {},
                         3200 if detail_requested(payload.get('question', '')) else 1200, payload.get('analysisFindings', {}))


def prepare_report_context(payload, system, schema, *, num_ctx=8192, num_predict=1536):
    facts = payload['facts']
    if not facts or len({f['id'] for f in facts}) != len(facts):
        raise ValueError('结论需要唯一可引用的账本证据')
    findings = payload.get('analysisFindings', {})
    for fact in facts:
        binding = fact.get('binding', {})
        if binding.get('primary') is True and binding.get('tool') in DECISION_TOOLS:
            finding = findings.get(binding.get('resultGroup'), {})
            if finding.get('scope') != binding.get('scope') or finding.get('tool') != binding.get('tool'):
                raise ValueError('关键判断与证据的范围或工具不一致')
    values, judgments = _catalog(facts), _judgments(facts, findings)
    char_limit = 3200 if detail_requested(payload.get('question', '')) else 1200
    common = {k: payload[k] for k in ('question', 'understanding', 'analysisScope', 'userConditions', 'reviewPriorities') if k in payload}
    common['answerRequirements'] = {'leadWithConclusion': True, 'targetCharacters': 500 if char_limit == 1200 else 1800,
                                    'maximumCharacters': char_limit, 'noAuditDump': True}
    common['judgments'] = [{k: j[k] for k in ('id', 'text', 'factIds', 'states')} for j in judgments]
    limit, estimate = _prompt_budget(system, schema, num_ctx, num_predict)
    limit -= 768  # Reserve a bounded correction instruction without changing reference IDs.
    mandatory, optional = _ordered(facts, judgments, payload.get('reviewPriorities', []))

    def project(selected):
        ids = {f['id'] for f in selected}
        bound_groups = {f.get('binding', {}).get('resultGroup') for f in selected}
        brief_findings = {key: {'scope': value.get('scope'), 'tool': value.get('tool'),
                               'states': value.get('states', {}),
                               **{field: value.get(field, [])[:2] for field in ('observations', 'limitations', 'actions')}}
                          for key, value in findings.items() if key in bound_groups}
        return {**common,
                'facts': [{'id': f['id'], 'text': (f.get('modelText') or f['text']) if len(f.get('modelText') or f['text']) <= 650
                           else '原文过长，当前仅提供已核算的数值及范围；不能补全未展示的含义。',
                           'binding': {k: v for k, v in f.get('binding', {}).items()
                                       if k in {'tool', 'scope', 'historyWindow', 'month', 'partial', 'metric',
                                                'aggregation', 'source', 'member', 'filters', 'primary'}}}
                          for f in selected],
                'values': [{k: value[k] for k in ('id', 'factId', 'label', 'display')} for value in values if value['factId'] in ids],
                'computedFindings': brief_findings}

    selected = list(mandatory)
    if estimate(project(selected)) > limit:
        raise ValueError('关键判断上下文超过预算；不能删除条件后强行给结论')
    for fact in optional:
        if len(selected) >= 36:
            break
        if estimate(project([*selected, fact])) <= limit:
            selected.append(fact)
    context_payload = project(selected)
    return ReportContext(facts, selected, values, judgments, context_payload,
                         {'estimatedPromptTokens': estimate(context_payload), 'promptBudgetTokens': limit,
                          'auditFactCount': len(facts), 'selectedFactCount': len(selected),
                          'omittedFactCount': len(facts) - len(selected)}, char_limit, findings)
