"""Render code-computed evidence into inert, reusable tables and charts."""

from datetime import date

from app.schemas.artifacts import public_artifacts
from app.tools.financial_analysis import shift_month


def cell(value):
    if value is None:
        return '未知'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (int, float)):
        return f'{value:,.2f}'.rstrip('0').rstrip('.')
    return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('\r', ' ').replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;')


def render_artifacts_markdown(artifacts):
    blocks = []
    for artifact in public_artifacts(artifacts):
        columns = artifact['columns']
        labels = [cell(column['label']) + (f"（{cell(column['unit'])}）" if column.get('unit') else '') for column in columns]
        lines = ['### ' + cell(artifact['title']), '', '| ' + ' | '.join(labels) + ' |',
                 '| ' + ' | '.join('---' for _ in columns) + ' |']
        for row in artifact['rows'][:60]:
            lines.append('| ' + ' | '.join(cell(row.get(column['key'])) for column in columns) + ' |')
        if len(artifact['rows']) > 60:
            lines.extend(['', '其余记录见完整数据表下载。'])
        lines.extend([''] + [cell(note) for note in artifact.get('notes', [])])
        blocks.append('\n'.join(lines).strip())
    return '\n\n'.join(blocks)


def build_artifacts(results, output=None, window=None):
    output, artifacts = output or {}, []
    metric = output.get('metric', 'all')

    def add(title, columns, rows, *, kind='table', x_key=None, series=None, notes=None):
        if rows:
            artifacts.append({'id': f'a{len(artifacts) + 1}', 'kind': kind, 'title': title,
                              'columns': [{'key': key, 'label': label, **({'unit': unit} if unit else {})}
                                          for key, label, unit in columns],
                              'rows': rows, 'notes': notes or [],
                              **({'xKey': x_key, 'series': series} if kind != 'table' else {})})

    for result in results:
        tool, e = result['tool'], result.get('evidence', {})
        scope = result.get('mode', '当前账本')
        if tool == 'forecast' and e.get('metric') == 'income' and e.get('sourceForecasts'):
            rows = [{'member': row.get('member', '归属未知'), 'source': row['source'],
                     'estimate': row.get('estimate'), 'lower': row.get('lower'), 'upper': row.get('upper'),
                     'basis': '；'.join(row.get('remarks', [])[:2]),
                     'status': row.get('status') or ('已剔除的非持续性入账' if row.get('excluded') else '历史模式延续的条件估计')}
                    for row in e['sourceForecasts']]
            rows.append({'member': '合计', 'source': '预测收入', 'estimate': e.get('estimate'),
                         'lower': e.get('lower'), 'upper': e.get('upper'), 'basis': '、'.join(e.get('baselineMonths', [])),
                         'status': '来源基准加总；上下界为历史情景，非置信区间'})
            add(f"{scope} {e['targetMonth']} 收入构成预测",
                [('member', '成员', ''), ('source', '来源', ''), ('estimate', '条件估计', '元'),
                 ('lower', '历史下界', '元'), ('upper', '历史上界', '元'), ('basis', '备注依据', ''), ('status', '说明', '')],
                rows, notes=[e.get('method', ''), '历史账本不能证明未来工作安排，缺失月份不按零处理。'])
        elif tool == 'sources' and output.get('includeSources', True) and not any(row['tool'] == 'forecast' for row in results):
            rows = [{'member': row.get('member', '归属未知'), 'source': row['source'],
                     'direction': '收入' if row['metric'] == 'income' else '支出',
                     'amount': row['amount'], 'count': row.get('recordCount'), 'remark': '；'.join(row.get('remarks', [])[:3])}
                    for row in e.get('sources', []) if metric == 'all' or row['metric'] == metric]
            add(f'{scope} 来源与备注依据', [('member', '成员', ''), ('source', '来源', ''), ('direction', '方向', ''),
                ('amount', '已记录合计', '元'), ('count', '笔数', '笔'), ('remark', '备注证据', '')], rows,
                notes=['仅据账本记录归类；备注是记录内容，不足以证明现实原因。'])
        elif tool == 'trends' and output.get('includeTrends', True):
            fields = [('income', 'incomeTotal', '收入'), ('expense', 'expenseTotal', '支出'),
                      ('net', 'netIncomeMinusExpense', '结余')]
            fields = [row for row in fields if metric == 'all' or row[0] == metric]
            months = {row['month']: row for row in e.get('months', [])}
            if not months:
                continue
            start = date.fromisoformat((window or {}).get('startDate', min(months) + '-01')).replace(day=1)
            end = date.fromisoformat((window or {}).get('endDate', max(months) + '-01')).replace(day=1)
            count = min(120, (end.year - start.year) * 12 + end.month - start.month + 1)
            rows = []
            for offset in range(count):
                month = shift_month(start, offset).strftime('%Y-%m')
                observed = months.get(month)
                rows.append({'month': month, **{key: observed.get(field) if observed else None for key, field, _ in fields},
                             'coverage': ('未完月份' if observed.get('partial') else '有账单记录') if observed else '没有记录，金额未知'})
            chart = output.get('chart', 'line')
            add(f"{scope} 月度{'、'.join(row[2] for row in fields)}趋势",
                [('month', '月份', '')] + [(key, label, '元') for key, _, label in fields] + [('coverage', '记录范围', '')],
                rows, kind=chart if chart in {'line', 'bar'} else 'table', x_key='month',
                series=[{'key': key, 'label': label} for key, _, label in fields],
                notes=['缺失月份显示为空缺；有账单记录也不能证明账本已覆盖全部收支。', e.get('coverageNote', '')])
        elif tool == 'changes' and e.get('sufficientData'):
            rows = []
            for direction, label in [('income', '收入'), ('expense', '支出')]:
                if metric not in {'all', direction}:
                    continue
                rows.extend({'direction': label, 'source': row['name'], 'before': row['before'],
                             'after': row['after'], 'change': row['change'], 'remark': '；'.join(row.get('afterRemarks', [])[:3])}
                            for row in e.get(direction + 'Drivers', []))
            add(f"{scope} {e['from']} 至 {e['to']} 变化来源",
                [('direction', '方向', ''), ('source', '来源', ''), ('before', '前期', '元'),
                 ('after', '后期', '元'), ('change', '变化', '元'), ('remark', '后期备注', '')], rows)
        elif tool == 'projection' and e.get('sufficientData'):
            start = date.fromisoformat(e['startMonth'] + '-01')
            rows = [{'month': shift_month(start, i).strftime('%Y-%m'), 'income': e['monthlyIncome'],
                     'expense': e['monthlyExpense'], 'net': e['monthlyNet'],
                     'cumulativeNet': round(e['monthlyNet'] * (i + 1), 2)} for i in range(min(600, e['months']))]
            add(f'{scope} 月度情景推演', [('month', '月份', ''), ('income', '收入', '元'), ('expense', '支出', '元'),
                ('net', '结余', '元'), ('cumulativeNet', '累计新增结余', '元')], rows,
                notes=['以上各月延续同一历史基准及用户假设，累计新增结余不是当前存款。'])
    return public_artifacts(artifacts)
