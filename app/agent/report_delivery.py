"""Keep explicit comparison requests in the bounded answer, without an audit dump."""

import json
import re


def _scope(binding, metric):
    return json.dumps([binding.get('scope'), metric, binding.get('historyWindow'), binding.get('filters') or [], binding.get('aggregateFilters') or {}], sort_keys=True, ensure_ascii=False)


def _in_requested_table(value, artifacts, scopes):
    binding = value['binding']
    period = binding.get('month') or binding.get('period')
    if not period:
        return False
    metric = 'expense' if '支出' in value['label'] else 'income' if '收入' in value['label'] else binding.get('metric')
    for table in artifacts:
        if table.get('kind') != 'table' or table.get('presentation') != 'requested':
            continue
        if len(scopes) > 1 and binding.get('scope', '') not in table.get('title', ''):
            continue
        for row in table.get('rows', []):
            if str(row.get('month', row.get('period', ''))) == str(period) and row.get(metric) == value['value']:
                return True
    return False


def delivery_requirements(question, facts, values, artifacts=()):
    # Remove only explicit requests not to display something, not "不要漏掉总额".
    question = re.sub(r'(?:不用|不要|不必|无需|不需要|别)(?:再|给我)?(?:列|展示|显示|提供|给出|说|报|写)[^，。；]*', '', question or '')
    expense = bool(re.search(r'支出|开销|花费|花钱', question))
    income = bool(re.search(r'收入|入账|进账', question))
    metric = 'expense' if expense and not income else 'income' if income and not expense else None
    per_period = bool(re.search(r'(?:每月|各月|每个[月期]|各期).{0,10}(?:总额|总支出|总收入|合计)|(?:分别|各自).{0,10}(?:总额|总支出|总收入)', question))
    both_changes = bool(re.search(r'增减', question) or
                        re.search(r'增加|上升|上涨', question) and re.search(r'减少|下降|下跌', question))
    totals, groups = {}, {}
    for fact in facts:
        binding = fact.get('binding', {})
        if binding.get('primary') is False:
            continue
        owned = [v for v in values if v['factId'] == fact['id']]
        for direction, label in [('expense', '支出'), ('income', '收入')]:
            if metric and metric != direction:
                continue
            selected, priority = [], 0
            if binding.get('evidenceRole') == 'period_total' and binding.get('metric') == direction:
                selected = [v for v in owned if v['label'] in {'前期总'+label, '后期总'+label}]
            elif binding.get('aggregation') == 'filtered_period_total':
                selected = [v for v in owned if v['label'] == '区间'+label+'小计'];priority=1
            elif binding.get('aggregation') == 'recorded_month_total':
                selected = [v for v in owned if v['label'] == '已记'+label];priority=2
            if per_period:
                for value in selected:
                    period = value['binding'].get('month') or binding.get('period') or value['label']
                    totals.setdefault((_scope(binding, direction), period), []).append((priority, [value]))
        if both_changes and binding.get('evidenceRole') == 'change_driver' and (not metric or binding.get('metric') == metric):
            delta = next((v['value'] for v in owned if v['label'] in {'金额变化', 'change'}), None)
            if not isinstance(delta, (int, float)) or not delta:
                continue
            direction = '增加' if delta > 0 else '减少'
            key = (_scope(binding, binding.get('metric')), direction)
            group = groups.setdefault(key, {'direction': direction, 'scope': binding.get('scope'),
                                          'metric': binding.get('metric'), 'candidates': []})
            group['candidates'].append({'factId': fact['id'], 'magnitude': abs(delta),
                'valueIds': [v['id'] for v in owned if v['label'].startswith(('金额变化', 'change'))]})
    required = []
    scopes = {f.get('binding', {}).get('scope') for f in facts}
    for rows in totals.values():
        best = min(priority for priority, _ in rows)
        for priority, selected in rows:
            if priority == best:
                required.extend({k: v[k] for k in ('id', 'factId', 'label')} for v in selected
                                if not _in_requested_table(v, artifacts, scopes))
    directions = []
    for group in groups.values():
        # Only the largest contribution in each requested direction is mandatory.
        candidate = max(group.pop('candidates'), key=lambda row: row['magnitude'])
        directions.append({**group, 'factId': candidate['factId'], 'valueIds': candidate['valueIds']})
    return {'requiredValues': required, 'changeDirections': directions}
