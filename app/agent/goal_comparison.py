"""Compare changed savings deadlines only within the same verified calculation."""

from copy import deepcopy

from app.agent.report_values import quantity as q
from app.tools.order_calculations import _round_money

REFERENCE = 'savingsDeadlineReference'


def _money(value):
    return f'{value:,.2f}'.rstrip('0').rstrip('.')


def add_savings_deadline_facts(facts, results, context, analysis_scope):
    candidates = [(index, row) for index, row in enumerate(results, 1)
                  if row['tool'] == 'savings' and not row.get('supplemental')]
    previous = context.pop(REFERENCE, None)
    if len(candidates) != 1:
        return
    index, result = candidates[0]
    evidence = result['evidence']
    if not evidence.get('sufficientData') or not evidence.get('months') or context.get('ambiguous'):
        return
    baseline = evidence.get('baseline') or {}
    # These are fresh tool inputs/results, never amounts parsed from assistant prose.
    signature = deepcopy({
        'scope': result.get('mode'), 'filters': result.get('filters', []),
        'historyWindow': result.get('historyWindow'),
        'sourceGroups': (analysis_scope or {}).get('sourceGroups', []),
        'startingBalance': evidence['startingBalance'], 'targetAmount': evidence['targetAmount'],
        'baselineMonths': baseline.get('baselineMonths'), 'targetMonth': baseline.get('targetMonth'),
        'monthlyEvidence': [row for row in baseline.get('monthlyEvidence', [])
                            if row.get('month') in baseline.get('baselineMonths', [])],
        'assumptions': baseline.get('assumptions', []),
        'baselineMonthlySaving': evidence['baselineMonthlySaving'],
    })
    months = evidence['months']
    current = {'signature': signature, 'months': months}
    context[REFERENCE] = current
    if not previous or previous.get('signature') != signature:
        return
    if previous.get('months') != months:
        old_months = previous.get('months')
    else:
        old_months = previous.get('comparisonFromMonths')
    if not isinstance(old_months, int) or old_months <= 0 or old_months == months:
        return
    current['comparisonFromMonths'] = old_months
    old_required = _round_money(evidence['remainingToTarget'] / old_months)
    required = evidence['requiredMonthlySaving']
    reduction = _round_money(old_required - required)
    change = ('要求降低，每月减少 ' + _money(reduction) + ' 元' if reduction > 0 else
              '要求提高，每月增加 ' + _money(-reduction) + ' 元' if reduction < 0 else '要求不变')
    binding = next((row['binding'] for row in facts if row['binding'].get('resultGroup') == f'g{index}'), None)
    if binding is None:
        return
    facts.append({
        'id': 'savings_deadline_comparison', 'required': True,
        'binding': {**binding, 'headline': True, 'aggregation': 'savings_deadline_comparison'},
        'text': f"【{result.get('mode', '当前账本')}】起点、总目标和收支基准不变，期限由 {old_months} 个月改为 {months} 个月，"
                f"目标所需月结余由 {_money(old_required)} 元变为 {_money(required)} 元，{change}。"
                f"按同一历史基准测算，月结余能力未变，仍为 {_money(evidence['baselineMonthlySaving'])} 元；"
                '期限调整不会自动增加收入或减少支出，未来实际能力仍取决于后续收支。',
        'values': [q('原期限', old_months, '个月'), q('当前期限', months, '个月'),
                   q('原目标月结余要求', old_required), q('当前目标月结余要求', required),
                   q('月结余要求减少额', reduction), q('同口径历史月结余能力', evidence['baselineMonthlySaving'])],
    })
