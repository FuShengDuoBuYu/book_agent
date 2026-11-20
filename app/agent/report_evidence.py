"""Separate complete audit evidence, bounded composition input and fixed decisions."""

from dataclasses import dataclass
from itertools import zip_longest
import re

from app.agent.analysis_context import DECISION_TOOLS
from app.agent.report_context import _prompt_budget
from app.agent.report_values import literal, value_catalog, QUANTITY_LITERAL


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


def conclusion_only(question):
    for match in re.finditer(r'一句(?:话|结论|总结)|只(?:要|说|给).{0,5}结论', question or ''):
        if not re.search(r'不是|不要|不只', (question or '')[max(0, match.start()-4):match.start()]):
            return True
    return False


def detail_requested(question):
    return not conclusion_only(question) and bool(re.search(r'详细|深入|完整报告|逐项|展开(?:分析|说明|讲)', question or ''))


def response_limit(question):
    return 250 if conclusion_only(question) else 3200 if detail_requested(question) else 1200


def judgment_leads(payload):
    question = payload.get('question', '')
    if re.search(r'多少|多久|要几个月|能否|能不能|是否足够|够不够|合适吗|能实现', question):
        return True
    return not bool(re.search(r'为什么|为啥|依据|方法|怎么算|怎么.*(?:算|判断)|如何.*(?:算|判断)|怎样.*(?:算|判断)|靠谱|可信|限制', question))


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
        anchors = []
        def take(label):
            for row in rows:
                for value in value_catalog([row]):
                    if value['label'] == label:
                        if row['id'] not in anchors:anchors.append(row['id'])
                        return value
            return None
        anchor = next((f for f in rows if f.get('binding', {}).get('headline')), rows[0])
        anchors.append(anchor['id'])
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
            if state is not None:
                projected = take('projectedBalance')
                text = ('按当前收支假设，给定期限内能达到目标。' if state else '按当前收支假设，给定期限内仍不能达到目标。')
                if projected:text += '期末预计余额为' + projected['display'] + '。'
            else:
                duration = take('estimatedMonthsToTarget')
                if duration is not None:
                    text = ('按给定余额，目标已经达到。' if duration['value'] == 0 else
                            '若当前收支模式持续，约需' + duration['display'] + '达到目标。')
                else:
                    text = '当前情景没有正的月度结余，无法按这一基准推算有限的达标时间。'
        elif tool == 'budget':
            if states.get('hasRecords') is False:
                text = '目标月尚未见账单，实际支出和预算余量未知，不能当作零支出或预算全部可用。'
            else:
                remaining, allowance = take('remaining'), take('dailyAllowance')
                text = ('按当前已记账单，预算余量为' + remaining['display'] + '。') if remaining else anchor['text']
                if allowance:text += '含今天的剩余时间内，日均可用额度为' + allowance['display'] + '；未记录支出会改变额度。'
        elif tool == 'optimization':
            gap, spend = take('候选项之外的改善缺口'), take('可核查月度候选开销')
            if gap and gap['value'] > 0 and spend:
                text = ('现有可核查支出上界为' + spend['display'] + '，即便全部调整，距每月改善目标仍差'
                        + gap['display'] + '；实际能调整多少还需确认。')
            else:
                text = next((line for line in finding.get('observations', []) if '目标' in line or '缺口' in line),
                            next(iter(finding.get('observations', [])), anchor['text']))
        elif tool == 'runway':
            duration = take('runwayMonths')
            text = ('按你设定的收支条件，这笔可动用资金约能维持' + duration['display'] + '。' if duration else
                    next((line for line in finding.get('observations', []) if '当前情景' in line), anchor['text']))
        else:
            text = anchor['text'].split('。')[0] + '。'
        result.append({'id': f'j{len(result) + 1}', 'text': text, 'factIds': list(dict.fromkeys(anchors)),
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
        labels += fact.get('literals', [])
        labels += re.findall(r'`+\s+([^`\n]+?)\s+`+', fact['text'])
        for label in dict.fromkeys(x for x in labels if isinstance(x, str) and x):
            if (QUANTITY_LITERAL.search(label) or re.search(r'[{}<>]|https?://', label)) and not re.fullmatch(r'\d{4}-\d{2}(?:-\d{2})?', label):
                values.append({'id': f'n{len(values) + 1}', 'factId': fact['id'], 'value': label,
                               'unit': 'label', 'label': '账单原文标签，不是金额', 'role': 'ledger_label',
                               'display': literal(label), 'binding': dict(binding)})
    for value in list(values):
        if value['unit'] == '元' and isinstance(value['value'], (int, float)) and value['value'] < 0:
            magnitude = abs(value['value'])
            values.append({**value, 'id': f'v{len(values) + 1}', 'value': magnitude,
                           'label': value['label'] + '（绝对值）', 'role': value['role'] + '（绝对值）',
                           'display': f'{magnitude:,.2f}'.rstrip('0').rstrip('.') + ' 元'})
    return values


def _ordered(facts, judgments, priorities, question=""):
    pinned = {key for j in [*judgments, *priorities] for key in j['factIds']}
    # Trend synthesis needs all returned month totals, not the generic coverage warning.
    pinned.update(f['id'] for f in facts if f.get('binding', {}).get('aggregation') == 'recorded_month_total')
    causes = {}
    for fact in facts:
        binding = fact.get('binding', {})
        if binding.get('primary') is False:
            continue
        typed_driver = binding.get('evidenceRole') == 'change_driver'
        if typed_driver or (binding.get('tool') == 'aggregate' and re.search(r'(?:category|source)=', fact['text'])):
            causes.setdefault((binding.get('resultGroup'), binding.get('metric') if typed_driver else None), []).append(fact)
    pinned.update(f['id'] for rows in causes.values() for f in rows[:2])
    if not judgment_leads({'question': question}):
        # Method questions need the actual source contributions and applied exclusions.
        method_facts = [f for f in facts if f.get('binding', {}).get('evidenceRole') in {'forecast_method', 'forecast_exclusions'}]
        source_facts = [f for f in facts if f.get('binding', {}).get('evidenceRole') == 'forecast_source']
        pinned.update(f['id'] for f in method_facts)
        pinned.update(f['id'] for f in source_facts[:4])
        pinned.update(f['id'] for f in source_facts if f.get('binding', {}).get('excluded'))
    mandatory = [f for f in facts if f['id'] in pinned]
    groups, warnings = {}, []
    for fact in facts:
        if fact['id'] in pinned:
            continue
        binding = fact.get('binding', {})
        has_data = any(v.get('value') is not None and v.get('unit', '元') not in {'date', 'month'} for v in fact.get('values', []))
        generic = any(phrase in fact['text'] for phrase in ('来源摘要另有', '详细表省略', '有记录月份只能说明', '本次现金流完整月份均值使用'))
        if not has_data or generic:
            warnings.append(fact)
        else:
            groups.setdefault(binding.get('resultGroup', fact['id']), []).append(fact)
    optional = [f for batch in zip_longest(*groups.values()) for f in batch if f is not None]
    return mandatory, [*optional, *warnings]


def _compact(value):
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items()
                if key not in {'sourceKeys', 'sourceKey', 'sourceGroups', 'queryIds',
                               'sourceGroupingScope', 'sourceGroupingScopes', 'sourceGroupScope'}
                and item is not None and item != '' and item != [] and item != {}}
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def audit_report_context(payload):
    facts = payload.get('facts', [])
    return ReportContext(facts, [], _catalog(facts), _judgments(facts, payload.get('analysisFindings', {})),
                         {k: payload[k] for k in ('question', 'reviewPriorities') if k in payload}, {},
                         response_limit(payload.get('question', '')), payload.get('analysisFindings', {}))


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
    char_limit = response_limit(payload.get('question', ''))
    common = _compact({k: payload[k] for k in ('question', 'understanding', 'analysisScope', 'userConditions', 'reviewPriorities', 'asOfDate') if k in payload})
    if 'reviewPriorities' in common:
        common['reviewPriorities'] = [{k: p[k] for k in ('title', 'reason', 'factIds') if k in p} for p in common['reviewPriorities']]
    if isinstance(common.get('analysisScope'), dict):
        for task in common['analysisScope'].get('primaryTasks', []):
            task.pop('aggregate', None)  # The report uses computed, scoped facts, not executable aggregation plans.
    common['answerRequirements'] = {'leadWithConclusion': True,
                                    'targetCharacters': 150 if char_limit == 250 else 500 if char_limit == 1200 else 1800,
                                    'maximumCharacters': char_limit, 'onlyConclusion': char_limit == 250,
                                    'leadWithJudgment': judgment_leads(payload),
                                    'noAuditDump': True}
    common['judgments'] = [{k: j[k] for k in ('id', 'text', 'factIds', 'states')} for j in judgments]
    limit, estimate = _prompt_budget(system, schema, num_ctx, num_predict)
    limit -= 768  # Reserve a bounded correction instruction without changing reference IDs.
    mandatory, optional = _ordered(facts, judgments, payload.get('reviewPriorities', []), payload.get('question', ''))

    def project(selected):
        ids = {f['id'] for f in selected}
        bound_groups = {f.get('binding', {}).get('resultGroup') for f in selected}
        brief_findings = {key: {'scope': value.get('scope'), 'tool': value.get('tool'),
                               'states': value.get('states', {}),
                               **{field: value.get(field, [])[:2] for field in ('observations', 'limitations', 'actions')}}
                          for key, value in findings.items() if key in bound_groups}
        supplied = []
        cap = 18 if detail_requested(payload.get('question', '')) else 8
        for fact in selected:
            owned = [v for v in values if v['factId'] == fact['id']]
            scalar = sorted((v for v in owned if v['unit'] != 'label'), key=lambda v: v['unit'] in {'date', 'month'})
            supplied.extend([*scalar[:cap], *(v for v in owned if v['unit'] == 'label')])
        return {**common,
                'facts': [{'id': f['id'], 'text': (f.get('modelText') or f['text']) if len(f.get('modelText') or f['text']) <= 650
                           else '原文过长，当前仅提供已核算的数值及范围；不能补全未展示的含义。',
                           'binding': {k: v for k, v in f.get('binding', {}).items() if v is not None and v != []
                                       if k in {'tool', 'scope', 'historyWindow', 'month', 'partial', 'metric',
                                                'aggregation', 'evidenceRole', 'excluded', 'category', 'source', 'member', 'filters', 'primary'}}}
                          for f in selected],
                'values': {v['id']: v['factId'] + ' ' + v['label'] + ' = ' + v['display'] for v in supplied},
                'computedFindings': brief_findings,
                'evidenceCoverage': {'auditFactCount': len(facts), 'contextFactCount': len(selected),
                                     'omittedForContext': len(facts) - len(selected)}}

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
