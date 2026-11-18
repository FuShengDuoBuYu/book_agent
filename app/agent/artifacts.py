"""Render code-computed evidence into inert, reusable tables and charts."""

import re
from datetime import date

from app.schemas.artifacts import public_artifacts
from app.tools.financial_analysis import shift_month
from app.tools.source_analysis import REMARK_LIMIT, money


METRIC_LABELS = {'income': '收入', 'expense': '支出', 'net': '结余'}
SOURCE_STATUS_LABELS = {
    'observed': '基准月份均有记录',
    'new': '仅最近基准月份有记录',
    'sparse': '部分基准月份有记录',
    'inactive': '基准月份未记录该来源',
    'excluded': '已从持续性收入预测中剔除',
}
REMARK_COVERAGE = f'每个来源展示至多 {REMARK_LIMIT} 条去重备注，较长备注保留前 160 字。'


def cell(value, *, literal=False):
    if value is None:
        return '未知'
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (int, float)):
        return f'{value:,.2f}'.rstrip('0').rstrip('.')
    text = str(value).replace('\r', ' ').replace('\n', ' ')
    if literal:
        if not text.strip():
            return ''
        # GFM splits table cells before parsing their inline Markdown.
        text = text.replace('|', '\\|')
        fence = '`' * (1 + max(map(len, re.findall(r'`+', text)), default=0))
        return f'{fence} {text} {fence}'
    return text.replace('\\', '\\\\').replace('|', '\\|').replace('<', '&lt;').replace('>', '&gt;')


def render_artifacts_markdown(artifacts):
    blocks = []
    for artifact in public_artifacts(artifacts):
        columns = artifact['columns']
        labels = [cell(column['label']) + (f"（{cell(column['unit'])}）" if column.get('unit') else '') for column in columns]
        lines = ['### ' + cell(artifact['title']), '', '| ' + ' | '.join(labels) + ' |',
                 '| ' + ' | '.join('---' for _ in columns) + ' |']
        for row in artifact['rows'][:60]:
            lines.append('| ' + ' | '.join(cell(row.get(column['key']), literal=True) for column in columns) + ' |')
        if len(artifact['rows']) > 60:
            lines.extend(['', '其余记录见完整数据表下载。'])
        lines.extend([''] + [cell(note) for note in artifact.get('notes', [])])
        blocks.append('\n'.join(lines).strip())
    return '\n\n'.join(blocks)


def build_artifacts(results, output=None, window=None):
    output, artifacts = output or {}, []
    metric = output.get('metric', 'all')
    chart = output.get('chart', 'none')
    chart_target = output.get('chartTarget', 'trends')
    include_trends = output.get('includeTrends', chart_target == 'trends')
    include_lineage = output.get('includeLineage', False)
    forecast_scopes = {row.get('mode', '当前账本') for row in results if row['tool'] == 'forecast'}

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
        kind = chart if tool == chart_target and chart in {'line', 'bar'} else 'table'
        if tool == 'forecast':
            forecast_metric = e.get('metric')
            components = e.get('sourceForecasts') if forecast_metric == 'income' else None
            if e.get('sufficientData') and forecast_metric in METRIC_LABELS and metric in {'all', forecast_metric} and (
                    kind != 'table' or (not components and (output.get('table') or output.get('export')))):
                label = METRIC_LABELS[forecast_metric]
                add(f"{scope} {e['targetMonth']} {label}预测",
                    [('month', '目标月份', ''), ('estimate', '条件估计', '元'),
                     ('lower', '历史下界', '元'), ('upper', '历史上界', '元')],
                    [{'month': e['targetMonth'], **{key: e.get(key) for key in ('estimate', 'lower', 'upper')}}],
                    kind=kind, x_key='month', series=[{'key': 'estimate', 'label': f'{label}条件估计'}],
                    notes=[e.get('method', ''), '上下界为历史情景。'])
            if components:
                rows = [{'member': row.get('member', '归属未知'), 'source': row['source'],
                         'historicalAmount': row.get('amount'),
                         'estimate': row.get('estimate'), 'lower': row.get('lower'), 'upper': row.get('upper'),
                         'basis': '；'.join(row.get('remarks', [])),
                         'status': SOURCE_STATUS_LABELS.get(
                             'excluded' if row.get('excluded') else row.get('status'),
                             '历史模式延续的条件估计')}
                        for row in components]
                rows.append({'member': '合计', 'source': '预测收入', 'estimate': e.get('estimate'),
                             'lower': e.get('lower'), 'upper': e.get('upper'), 'basis': '、'.join(e.get('baselineMonths', [])),
                             'status': '来源条件估计加总，上下界为历史情景'})
                add(f"{scope} {e['targetMonth']} 收入构成预测",
                    [('member', '成员', ''), ('source', '来源', ''), ('historicalAmount', '历史入账', '元'),
                     ('estimate', '条件估计', '元'), ('lower', '历史下界', '元'), ('upper', '历史上界', '元'),
                     ('basis', '备注依据', ''), ('status', '说明', '')],
                    rows, notes=[e.get('method', ''), REMARK_COVERAGE,
                                 '历史入账包含已剔除来源在观察期内的记录。',
                                 '全月缺少账单时金额未知。未来工作安排需由用户确认。'])
        elif tool == 'sources' and (
                include_lineage or (output.get('includeSources', True) and scope not in forecast_scopes)):
            rows = [{'member': row.get('member', '归属未知'), 'source': row['source'],
                     'direction': '收入' if row['metric'] == 'income' else '支出',
                     'amount': row['amount'], 'count': row.get('recordCount'), 'remark': '；'.join(row.get('remarks', []))}
                    for row in e.get('sources', []) if metric == 'all' or row['metric'] == metric]
            notes = [REMARK_COVERAGE]
            if omitted := e.get('omittedSourceCount', 0):
                notes.append(f'另有 {omitted} 个来源超出目录展示范围。')
            add(f'{scope} 来源与备注依据', [('member', '成员', ''), ('source', '来源', ''), ('direction', '方向', ''),
                ('amount', '已记录合计', '元'), ('count', '笔数', '笔'), ('remark', '备注证据', '')], rows,
                notes=notes)
        elif tool == 'trends' and (kind != 'table' or include_trends):
            fields = [('income', 'incomeTotal', '收入'), ('expense', 'expenseTotal', '支出'),
                      ('net', 'netIncomeMinusExpense', '结余')]
            fields = [row for row in fields if metric == 'all' or row[0] == metric]
            months = {row['month']: row for row in e.get('months', [])}
            if not months:
                continue
            start = date.fromisoformat((window or {}).get('startDate', min(months) + '-01')).replace(day=1)
            end = date.fromisoformat((window or {}).get('endDate', max(months) + '-01')).replace(day=1)
            count = min(120, (end.year - start.year) * 12 + end.month - start.month + 1)
            rows, missing, partial = [], [], []
            for offset in range(count):
                month = shift_month(start, offset).strftime('%Y-%m')
                observed = months.get(month)
                if observed is None:
                    missing.append(month)
                elif observed.get('partial'):
                    partial.append(month)
                rows.append({'month': month, **{key: observed.get(field) if observed else None for key, field, _ in fields},
                             'coverage': ('部分月份数据' if observed.get('partial') else '有账单记录') if observed else '没有记录，金额未知'})
            label = '、'.join(field_label for _, _, field_label in fields)
            notes = []
            if missing:
                notes.append(f"{'、'.join(missing)} 缺少账单记录，{label}金额未知。")
            if partial:
                notes.append(f"{'、'.join(partial)} 按本次查询范围内的部分月份{label}记录汇总。")
            add(f"{scope} 月度{label}趋势",
                [('month', '月份', '')] + [(key, label, '元') for key, _, label in fields] + [('coverage', '记录范围', '')],
                rows, kind=kind, x_key='month',
                series=[{'key': key, 'label': label} for key, _, label in fields],
                notes=notes)
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
            fields = [(key, label) for key, label in METRIC_LABELS.items() if metric in {'all', key}]
            amounts = {key: e['monthly' + key.capitalize()] for key, _ in fields}
            columns = [('month', '月份', '')] + [(key, label, '元') for key, label in fields]
            rows = []
            for index in range(min(600, e['months'])):
                row = {'month': shift_month(start, index).strftime('%Y-%m'), **amounts}
                if 'net' in amounts:
                    row['cumulativeNet'] = money(amounts['net'] * (index + 1))
                rows.append(row)
            if 'net' in amounts:
                columns.append(('cumulativeNet', '累计新增结余', '元'))
            add(f'{scope} 月度情景推演', columns, rows,
                kind=kind, x_key='month', series=[{'key': key, 'label': label} for key, label in fields],
                notes=['各月延续同一历史基准及用户假设。累计新增结余表示推演期间的净现金流。'])
    return public_artifacts(artifacts)
