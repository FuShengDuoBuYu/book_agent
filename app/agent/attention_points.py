"""Select explicitly requested review priorities from audited results of one report."""

import re

from app.agent.report_composition import grounded_analysis
from app.agent.report_values import literal


_COUNT = r'([1-6一二两三四五六])'
_REQUEST = re.compile(
    rf'最值得(?:关注|注意)的?\s*{_COUNT}\s*(?:个|项|条)?\s*(?:问题|点|事项)'
    rf'|列(?:出|举)?\s*{_COUNT}\s*(?:个|项|条|点)?\s*主要问题')
_NUMBERS = dict(zip('一二两三四五六', (1, 2, 2, 3, 4, 5, 6)))


def requested_attention_count(question):
    for match in _REQUEST.finditer(question or ''):
        if re.search(r'(?:不要|无需|不用|不必|别)\s*$', question[:match.start()]):
            continue
        value = match[1] or match[2]
        return int(value) if value.isdigit() else _NUMBERS[value]
    return None


def _money(value):
    return f'{value:,.2f}'.rstrip('0').rstrip('.') + ' 元'


def _candidates(tool, e):
    """Yield priority, aspect, title, reason, and the typed value anchoring the reason."""
    if e.get('sufficientData') is False:
        return
    if tool == 'purchase':
        for field, title, shortage in [('cashAfterPurchase', '购买资金', '现有可动用余额不足以支付购买价'),
                                       ('reserveMargin', '购买后的应急储备', '购买后余额低于给定的应急储备底线')]:
            value = e.get(field)
            if value is not None and value < 0:
                yield 0, 'purchase', title, f'{shortage}，按给定条件相差{_money(-value)}。', field, value, None
                break
    elif tool == 'budget' and e.get('hasRecords') and (e.get('overBudget') or 0) > 0:
        yield (1, 'budget', '目标月预算', f"目标月已记支出超出预算{_money(e['overBudget'])}，需要核对剩余安排。",
               'remaining', e['remaining'], None)
    elif tool == 'savings':
        projected, target = e.get('projectedBalance'), e.get('targetAmount')
        if projected is not None and target is not None and projected < target:
            yield (2, 'goal', '储蓄目标进度', f"在给定期限和收支假设下，期末预计余额距目标还差{_money(target - projected)}。",
                   'projectedBalance', projected, None)
    elif tool == 'runway' and (e.get('monthlyDeficit') or 0) > 0:
        value = e['monthlyDeficit']
        yield 2, 'scenario_gap', '情景下的月度资金缺口', f'按给定收支变化，月缺口为{_money(value)}，需要用可动用余额覆盖。', 'monthlyDeficit', value, None
    elif tool == 'optimization' and (e.get('gapBeyondReviewableSpend') or 0) > 0:
        value = e['gapBeyondReviewableSpend']
        yield 2, 'goal', '结余改善目标', f'即使用尽可核查支出的上界，距离改善目标仍差{_money(value)}。', '候选项之外的改善缺口', value, None
    elif tool == 'cashflow':
        avg = e.get('completeMonthAverage') or {}
        if avg.get('months') and avg.get('net') is not None and avg['net'] < 0:
            value = avg['net']
            yield (3, 'cashflow', '历史月度收支', f'有记录的完整月份中，月均已记支出比月均收入多{_money(-value)}。',
                   '完整月份月均结余', value, None)
    if tool == 'changes':
        rows = [r for r in e.get('expenseDrivers', []) if r.get('change') and r.get('name')]
        total = e.get('expenseChange')
        aligned = [r for r in rows if total and total * r['change'] > 0]
        if aligned or rows:
            row = max(aligned or rows, key=lambda r: abs(r['change']))
            value, name = row['change'], row['name']
            direction = '增加' if value > 0 else '减少'
            reason = f"对比期{literal(name)}已记支出{direction}{_money(abs(value))}，是已列类别中值得核对的金额变动。"
            yield 4, 'expense_change', '支出变化来源', reason, '金额变化', value, name
    if tool in {'cashflow', 'income_stability'}:
        rows = e.get('incomeSources' if tool == 'cashflow' else 'sources', [])
        for row in rows:
            share, name = row.get('sharePercent'), row.get('source')
            if name and share is not None and share > 50:
                basis = '查询期已记收入' if tool == 'cashflow' else '所统计完整月份的收入'
                yield (5, 'concentration', '收入来源结构', f'{literal(name)}占{basis}的大部分，其到账变化值得单独核查。',
                       'sharePercent', share, name)
                break
    if tool in {'anomalies', 'data_quality'}:
        duplicates = e.get('possibleDuplicates', [])
        if duplicates:
            yield (6, 'duplicates', '疑似重复记录', '检测到可能重复的记录，核对原始账单可确认是否影响合计。',
                   '疑似重复组数', len(duplicates), None)
        fields = [('missingPaymentMethodCount', '支付方式'), ('missingCategoryCount', '分类')]
        missing = [(key, name) for key, name in fields if (e.get(key) or 0) > 0]
        if missing:
            key = missing[0][0]
            yield (7, 'missing_fields', '账单字段核查', '有记录缺少' + '、'.join(v for _, v in missing) + '，会影响按相应字段归集。',
                   key, e[key], None)
        sparse = [r['category'] for r in e.get('insufficientCategories', []) if r.get('category')]
        if sparse:
            names = '、'.join(literal(name) for name in sparse[:3])
            yield (8, 'sample', '同类记录的可比性', f'{names}的同类样本不足，暂时无法据此判断金额是否异常。',
                   '疑似重复组数', len(duplicates), None)


def build_attention_points(question, results, facts, findings):
    count = requested_attention_count(question)
    if not count:
        return []
    candidates = []
    multiple_scopes = len({result.get('mode', '当前账本') for result in results}) > 1
    for index, result in enumerate(results, 1):
        group = f'g{index}'
        scope = result.get('mode', '当前账本')
        tool = result['tool']
        finding = findings.get(group) or {}
        if finding.get('scope') != scope or finding.get('tool') != tool:
            continue
        if not any(any(finding.get(k) or []) for k in ('observations', 'limitations', 'actions')):
            continue
        expected = {'resultGroup': group, 'scope': scope, 'tool': tool,
                    'queryIds': result.get('queryIds', []), 'filters': result.get('filters', []),
                    'historyWindow': None if tool == 'budget' else result.get('historyWindow')}
        scoped = [f for f in facts if all(f.get('binding', {}).get(k) == v for k, v in expected.items())]
        for rank, aspect, title, reason, label, value, name in _candidates(tool, result.get('evidence') or {}):
            # Older driver facts lack a category binding. Require both the exact quoted
            # source and a matching typed value, even if the outline omitted other facts.
            proof = next((f for f in scoped if (not name or any('“' + s + '”' in f['text'] for s in (name, literal(name))))
                          and any(v.get('label') == label and v.get('value') == value for v in f.get('values', []))), None)
            if proof:
                ids = [proof['id']]
                if multiple_scopes:
                    reason = f'在{literal(scope)}中，{reason}'
                candidates.append((rank, bool(result.get('supplemental')), aspect,
                                   {'title': title, 'reason': reason, 'factIds': ids,
                                    'analysis': grounded_analysis(ids, scoped, findings)}))
    chosen = {}
    for _, _, aspect, point in sorted(candidates, key=lambda row: row[:2]):
        chosen.setdefault(aspect, point)
        if len(chosen) == count:
            break
    return list(chosen.values())
