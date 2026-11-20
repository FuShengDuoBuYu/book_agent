"""Compose scoped conclusions while retaining full computed audit evidence."""

import asyncio
import json
import re
from datetime import date

from app.core.config import get_settings
from app.core.llm import get_structured_model
from app.core.openai_adapter import response_format
from app.schemas.report import ReportDraft
from pydantic import ValidationError
from time import perf_counter
from app.agent.analysis_findings import build_analysis_findings
from app.agent.attention_points import build_attention_points, requested_attention_count
from app.agent.report_composition import (compose_report, grounded_analysis, validate_draft, render_report,
                                          fallback_report, answer_schema, REPORT_PROMPT, REPORT_TOKENS)
from app.agent.report_evidence import prepare_report_context, audit_report_context
from app.tools.financial_analysis import shift_month
from app.agent.report_values import literal, quantity as q, sanitize_fact
from app.agent.goal_comparison import add_savings_deadline_facts


INPUT_NAMES = {'startingBalance': '当前可动用余额', 'targetAmount': '希望达到的存款目标',
               'months': '测算期限（月数）', 'changes': '收入减少的来源和幅度',
               'source': '该来源的历史金额或明确变化金额', 'purchaseAmount': '购买金额',
               'reserveAmount': '需要保留的应急储备', 'budgetAmount': '预算金额'}
def money(value):
    return f'{value:,.2f}'.rstrip('0').rstrip('.') if isinstance(value, (int, float)) else '未知'


def _ledger_literals(value):
    labels = set()
    def visit(item, field=''):
        if isinstance(item, dict):
            for key, child in item.items():visit(child, key)
        elif isinstance(item, list):
            for child in item:visit(child, field)
        elif isinstance(item, str) and field in {'source', 'member', 'name', 'category', 'remark', 'remarks', 'afterRemarks'}:
            if item and len(item) <= 160:labels.add(item)
    visit(value)
    return labels


def build_facts(results):
    from app.agent.evidence_pipeline import computed_metric

    facts = []

    def add(text, required=False, values=(), model_text=None, binding=None):
        values = list(values)
        inputs = {
            'savings': ('startingBalance', 'targetAmount', 'months'),
            'purchase': ('startingBalance', 'purchaseAmount', 'reserveAmount'),
            'runway': ('startingBalance',), 'budget': ('budgetAmount',),
            'optimization': ('monthlyImprovement',), 'projection': ('startingBalance', 'months'),
        }
        # Verified parameters belong to the whole calculation, even when the
        # explanation cites a different result of that same calculation.
        values.extend(q(INPUT_NAMES.get(key, key), e[key], '个月' if key == 'months' else '元')
                      for key in inputs.get(tool, ()) if e.get(key) is not None)
        for assumption in e.get('assumptions', e.get('baseline', {}).get('assumptions', [])):
            percent = assumption.get('percent')
            if percent is not None:
                values.extend([q('用户假设变化比例', percent, '%'), q('用户假设变化比例幅度', abs(percent), '%')])
            if assumption.get('amount') is not None:
                values.append(q('用户假设每次变化', assumption['amount']))
                values.append(q('用户假设每次变化幅度', abs(assumption['amount'])))
                values.append(q('用户假设变化次数', assumption.get('count', 1), '次'))
        if tool == 'forecast':
            values.append(q('预测跨度', 1, '个月'))
        if tool == 'forecast' and required:
            values.extend(q('参考月份', month, 'month') for month in e.get('baselineMonths', []))
            values.append(q('参考月份数', len(e.get('baselineMonths', [])), '个月'))
            values.extend(q('历史观察月份', month, 'month') for month in e.get('observedMonths', []))
            values.extend(q('历史观察月份数', len(e.get('observedMonths', [])), unit) for unit in ('个', '个月'))
        if text == e.get('computedConclusion'):
            if tool == 'cashflow':
                average = e.get('completeMonthAverage', {})
                values.extend(q(label, average.get(key)) for key, label in
                              [('income', '完整月份月均收入'), ('expense', '完整月份月均支出'), ('net', '完整月份月均结余')])
                values.append(q('完整月份结余率', average.get('netIncomeRatioPercent'), '%'))
                values.extend(q('参考月份', month, 'month') for month in average.get('months', []))
                values.extend(q('完整月份数', len(average.get('months', [])), unit) for unit in ('个', '个月'))
            else:
                fields = {'scenario': ['income', 'expense', 'net', 'baselineNet', 'netChange'],
                          'savings': ['projectedBalance', 'targetAmount']}.get(tool, [])
                values.extend(q(key, e.get(key)) for key in fields)
                if tool == 'scenario':
                    values.append(q('结余占收入', e.get('netIncomeRatioPercent'), '%'))
                if tool == 'savings':
                    values.append(q('目标期限', e.get('months'), '个月'))
        fact = {'id': f'f{len(facts) + 1}', 'text': sanitize_fact(prefix + text, result),
                'required': required and not result.get('supplemental', False), 'values': values,
                'binding': {**base_binding, **(binding or {})}}
        fact['literals'] = sorted(label for label in result_literals if label in text)
        if tool in {'forecast', 'projection', 'scenario', 'savings', 'purchase', 'budget', 'runway', 'optimization'}:
            fact['binding']['headline'] = len(facts) - result_start < 2
        if tool == 'cashflow' and text == e.get('computedConclusion'):
            fact['binding'].update(aggregation='complete_month_average', months=e.get('completeMonthAverage', {}).get('months', []))
        if tool == 'forecast' and e.get('targetMonth'):
            fact['calendars'] = [e['targetMonth']]
        elif tool == 'projection' and e.get('startMonth') and e.get('months'):
            start = date.fromisoformat(e['startMonth'] + '-01')
            fact['calendars'] = [shift_month(start, index).strftime('%Y-%m') for index in range(e['months'])]
        elif tool == 'aggregate':
            fact['calendars'] = sorted({row[key] for row in e.get('coverage', []) for key in ('startDate', 'endDate') if row.get(key)})
        elif tool == 'changes':
            fact['calendars'] = [e[key] for key in ('from', 'to') if e.get(key)]
        if model_text:
            fact['modelText'] = sanitize_fact(prefix + model_text, result)
        facts.append(fact)

    for result_number, result in enumerate(results, 1):
        result_start = len(facts)
        tool, e = result['tool'], result['evidence']
        result_literals = _ledger_literals(e)
        scope = result.get('mode', '当前账本')
        base_binding = {key: value for key, value in {
            'resultGroup': f'g{result_number}', 'primary': not result.get('supplemental', False),
            'filters': result.get('filters', []), 'queryIds': result.get('queryIds', []),
            'tool': tool, 'scope': scope, 'metric': computed_metric(tool, e),
            'historyWindow': result.get('historyWindow') if tool != 'budget' else None,
            'month': e.get('month') if tool == 'budget' else None,
            'targetMonth': e.get('targetMonth') if tool == 'forecast' else None,
            **({key: e.get(key) for key in ('startMonth', 'endMonth', 'months')} if tool == 'projection' else {}),
            'aggregation': {'sources': 'recorded_source_total', 'forecast': 'conditional_month_forecast',
                            'projection': 'conditional_period_projection', 'aggregate': 'filtered_aggregation',
                            'changes': 'recorded_period_comparison'}.get(tool, 'computed_evidence'),
        }.items() if value is not None}
        if e.get('baseline', {}).get('sufficientData') and tool in {'projection', 'scenario', 'savings', 'optimization', 'runway'}:
            base_binding['baselineAggregation'] = 'source_month_medians'
        filters = result.get('filters') or []
        narrowed = [f"{row.get('category', '不限')}／{row.get('remark') or '全部备注'}" for row in filters
                    if row.get('category') not in (None, '不限') or row.get('remark')]
        prefix = f'【{scope}' + ('，' + '；'.join(narrowed) if narrowed else '') + '】'
        missing = e.get('missingInputs', [])
        if missing:
            add('还需确认：' + '、'.join(INPUT_NAMES.get(key, key) for key in missing) + '。' + e.get('reason', ''), True)
        if e.get('sufficientData') is False:
            if not missing:
                add(e.get('reason') or '完整历史样本不足，当前不能支持这项测算。', True)
            if tool == 'purchase' and e.get('purchaseAmount') is not None:
                add(f"计划购买金额 {money(e['purchaseAmount'])} 元。月度结余不是现有存款，暂不能判断购买后是否影响生活。", True, values=[q('购买金额', e['purchaseAmount'])])
            continue
        if tool == 'aggregate':
            observed = any(row['observed'] for row in e['coverage'])
            totals = e['totals']
            labels = {'income': '收入', 'expense': '支出', 'net': '净额'}
            measures = ('income', 'expense', 'net') if e['metric'] in {'all', 'net'} else (e['metric'],)
            partial = any(row.get('queryCoverage', 'complete') != 'complete' for row in e['coverage'])
            basis = '仅已查询部分' if partial else '所选区间并集'
            if observed:
                amounts = '、'.join(f"{labels[name]} {money(totals[name])} 元" for name in measures)
                add(f"按{basis}与筛选条件去重后，已记{amounts}，共 {totals['count']} 笔；区间重叠的账单只在此总计中计入一次。", True,
                    values=[q(name, totals[name]) for name in measures] + [q('纳入笔数', totals['count'], '笔')])
            else:
                add('所选区间完全没有已记录账单，实际收支未知，不能将空结果当成现实收支为零。', True)
            if e.get('appliedFilters'):
                audit = e['filterAudit']['excludedByFilters']
                add(f"已应用筛选 {literal(json.dumps(e['appliedFilters'], ensure_ascii=False))}，由此排除 {audit['count']} 笔、其中支出 {money(audit['expense'])} 元、收入 {money(audit['income'])} 元。筛选后统计仅代表选中的账单。", True,
                    values=[q('被条件排除的笔数', audit['count'], '笔'), q('被条件排除的支出', audit['expense']), q('被条件排除的收入', audit['income'])])
            for coverage in e['coverage']:
                query_status = coverage.get('queryCoverage', 'complete')
                if query_status != 'complete':
                    add(f"区间 {literal(coverage['period'])} 的查询覆盖为" + ('部分已查询，剩余日期尚未查询，不能当作缺账或零金额。' if query_status == 'partial' else '未查询，金额未知，不能当作没有记账。'), True)
                add(f"区间 {literal(coverage['period'])}：{coverage['startDate']} 至 {coverage['endDate']}，" +
                    (f"已查询部分记录 {coverage['recordCount']} 笔，其中 {coverage['matchedCount']} 笔符合所选指标和筛选；有记录仍不能证明全部现实交易都已记账。" if coverage['observed'] else
                     '已查询范围内未见记录，金额和变化未知。' if query_status != 'complete' else '完整查询后未见账单，现实金额和变化仍未知。'),
                    not coverage['observed'], values=[q('区间开始', coverage['startDate'], 'date'), q('区间结束', coverage['endDate'], 'date'),
                        q('区间已记笔数', coverage['recordCount'], '笔'), q('筛选匹配笔数', coverage['matchedCount'], '笔')])
            for row in e.get('periodTotals', []):
                if row.get('count') is None:
                    continue  # Coverage facts above explain why this complete total is unknown.
                amounts = '、'.join(f"{labels[name]} {money(row[name])} 元" for name in measures)
                add(f"区间 {literal(row['period'])} 按当前筛选的完整已记小计：{amounts}，共 {row['count']} 笔。"
                    '重叠日期在各区间内分别统计，不能再将各期小计相加当作并集总额。', True,
                    binding={'period': row['period'], 'aggregation': 'filtered_period_total'},
                    values=[q('区间' + labels[name] + '小计', row[name]) for name in measures]
                        + [q('区间笔数', row['count'], '笔')])
            for row in e.get('periodComparisons', []):
                if row.get('change') is None:
                    continue
                add(f"筛选后区间总额比较，{literal(row['beforePeriod'])} 至 {literal(row['afterPeriod'])}的已记{labels[row['metric']]}"
                    f"从 {money(row['before'])} 元变为 {money(row['after'])} 元，变化 {money(row['change'])} 元。", True,
                    binding={key: row[key] for key in ('metric', 'beforePeriod', 'afterPeriod')},
                    values=[q('区间前期总额', row['before']), q('区间后期总额', row['after']),
                            q('区间总额变化', row['change']), q('区间总额变化率', row.get('changePercent'), '%')])
            for row in e['rows'][:8]:
                dimensions = '、'.join(f"{name}={literal(row[name])}" for name in e['dimensions'] if name in row) or '全部选中记录'
                amounts = '，'.join(f"{labels[name]} {money(row[name])} 元" for name in measures)
                values = [q(name, row[name]) for name in measures] + [q('账单笔数', row['count'], '笔')]
                detail = ''
                if row['meanTransactionAmount'] is not None:
                    detail = f"，每笔金额均值 {money(row['meanTransactionAmount'])} 元，中位数 {money(row['medianTransactionAmount'])} 元"
                    values.extend(q(name, row[name]) for name in ('meanTransactionAmount', 'medianTransactionAmount'))
                if row['sharePercent'] is not None:
                    detail += f"，占同区间筛选后该方向金额 {row['sharePercent']}%"
                    values.append(q('同区间筛选后方向占比', row['sharePercent'], '%'))
                if row.get('month'):
                    values.append(q('月份', row['month'], 'month'))
                add(f"{dimensions}：{amounts}，{row['count']} 笔{detail}。", values=values)
            for row in e['comparisons'][:4]:
                dimensions = '、'.join(f"{name}={literal(row[name])}" for name in e.get('comparisonDimensions', e['dimensions']) if name != 'period' and name in row) or '全部选中记录'
                if row['change'] is None:
                    add(f"{dimensions}，{literal(row['beforePeriod'])} 对比 {literal(row['afterPeriod'])}：至少一个区间未查全或没有已记账单，变化未知。", True)
                else:
                    add(f"{dimensions}，{literal(row['beforePeriod'])} 至 {literal(row['afterPeriod'])}的已记{labels[row['metric']]}从 {money(row['before'])} 元变为 {money(row['after'])} 元，变化 {money(row['change'])} 元。", True,
                        values=[q(name, row[name]) for name in ('before', 'after', 'change')] + [q('变化率', row.get('changePercent'), '%')])
            if e['omittedGroupCount'] or e['omittedComparisonCount']:
                add(f"详细表省略 {e['omittedGroupCount']} 个分组、{e['omittedComparisonCount']} 项比较；总计仍覆盖完整筛选范围，不能把展示行当作全部数据。", True,
                    values=[q('省略分组', e['omittedGroupCount'], '个'), q('省略比较', e['omittedComparisonCount'], '项')])
            add('账单笔数和每笔均额是记账统计，不能据此断定商品单价、购买数量或现实消费频率；同名来源是否同一业务仍需结合备注核查。')
        elif tool == 'cashflow':
            add(e['computedConclusion'], True)
            months = e.get('completeMonthAverage', {}).get('months', [])
            if months:
                add('本次现金流完整月份均值使用：' + '、'.join(months) + '；来源累计金额与占比则使用下述现金流取数窗口，不能与其他区间的累计金额混用。', True,
                    values=[q('完整月份', month, 'month') for month in months])
            window = result.get('historyWindow') or {}
            intervals = e.get('queryIntervals')
            if intervals is None and window.get('startDate') and window.get('endDate'):
                intervals = [window]
            dates = [q(label, interval[key], 'date') for interval in intervals or []
                     for key, label in [('startDate', '统计起日'), ('endDate', '统计止日')]]
            scope_text = ('本次现金流取数窗口 ' + '；'.join(row['startDate'] + ' 至 ' + row['endDate'] for row in intervals)
                          if intervals else '本次现金流已查账单范围（未限定起止日期）')
            denominator = (f"该窗口已记收入合计 {money(e['incomeTotal'])} 元"
                           if e.get('incomeTotal') is not None else '该窗口已记收入合计')
            for row in e.get('incomeSources', [])[:3]:
                share = (f"占{denominator}的 {row['sharePercent']}%" if row.get('sharePercent') is not None else '窗口收入为零，占比不适用')
                add(f"{scope_text}，收入来源“{row['source']}”累计 {money(row['amount'])} 元，{share}。",
                    values=[q('金额', row['amount']), q('sharePercent', row.get('sharePercent'), '%'), q('历史窗口收入合计', e.get('incomeTotal'))] + dates,
                    binding={'aggregation': 'recorded_window_source_total'})
            for row in e.get('expenseCategories', [])[:3]:
                share = (f"占{denominator}的 {row['shareOfIncomePercent']}%" if row.get('shareOfIncomePercent') is not None else '窗口收入为零，占收入比不适用')
                add(f"{scope_text}，“{row['name']}”支出 {money(row['amount'])} 元，{share}。",
                    values=[q('金额', row['amount']), q('shareOfIncomePercent', row.get('shareOfIncomePercent'), '%'), q('历史窗口收入合计', e.get('incomeTotal'))] + dates)
            add('分类占比反映已记录支出结构，不能单凭占比判断生活负担；资产与债务余额需要另行确认。')
        elif tool == 'trends':
            for row in e['months'][-6:]:
                add(f"{row['month']} 已记收入 {money(row['incomeTotal'])} 元，支出 {money(row['expenseTotal'])} 元，结余 {money(row['netIncomeMinusExpense'])} 元。" + ('此月尚未完整覆盖，不能与整月直接比较。' if row['partial'] else ''), values=[q('月份', row['month'], 'month'), q('已记收入', row['incomeTotal']), q('已记支出', row['expenseTotal']), q('已记结余', row['netIncomeMinusExpense'])],
                    binding={'aggregation': 'recorded_month_total', 'month': row['month'], 'partial': row['partial']})
            add('有记录月份只能说明已记账收支；缺失月份未知，不能按零补齐。', True)
        elif tool == 'sources':
            if not e.get('sources') and not e.get('memberTotals'):
                add('查询范围内未见可归集的该方向账单，现实金额与来源仍未知，不能把空结果当作现实收支为零。', True)
            for row in e.get('memberTotals', []):
                label = '收入' if row['metric'] == 'income' else '支出'
                member = row.get('member') or '归属未知的记录'
                if row.get('memberKey') in (None, 'member_unknown'):
                    member = '归属未知的记录'
                add(f"{member}在查询区间内的已记{label}小计 {money(row['amount'])} 元，共 {row['recordCount']} 笔。"
                    + ('已记收入包括该方向所有入账，不直接当作持续性收入。' if row['metric'] == 'income' else ''),
                    result.get('mode') == '家庭版',
                    binding={'aggregation': 'recorded_member_total', 'metric': row['metric'], 'member': member,
                             'memberKey': row.get('memberKey')},
                    values=[q('该成员查询期' + label + '小计', row['amount']), q('该成员' + label + '账单笔数', row['recordCount'], '笔')])
            for row in e.get('sources', [])[:8]:
                label = '收入' if row['metric'] == 'income' else '支出'
                summary = f"{row.get('member', '归属未知')}的{label}来源“{row['source']}”已记录 {money(row['amount'])} 元，备注依据："
                preview = [str(remark)[:160] for remark in row.get('remarks', [])[:4]]
                add(summary + literal(json.dumps(row.get('remarks', []), ensure_ascii=False)) + '。',
                    binding={**{key: row[key] for key in ('metric', 'member', 'memberKey', 'source', 'sourceKey') if key in row},
                             'originalRemarks': preview, 'omittedRemarkCount': max(0, len(row.get('remarks', [])) - len(preview))},
                    model_text=summary + literal(json.dumps(preview, ensure_ascii=False)) + '。',
                    values=[q('金额', row['amount']), q('账单笔数', row.get('recordCount'), '笔')] + [q('账单月份', month, 'month') for month in row.get('monthlyAmounts', {})])
            if e.get('omittedSourceCount'):
                add(f"来源摘要另有 {e['omittedSourceCount']} 项未展开，不能据摘要断言不存在其他来源。", True, values=[q('省略来源', e['omittedSourceCount'], '项')])
        elif tool == 'changes':
            add(f"从 {e['from']} 到 {e['to']}，收入变化 {money(e['incomeChange'])} 元，支出变化 {money(e['expenseChange'])} 元，结余变化 {money(e['netChange'])} 元。", True, values=[q('from', e['from'], 'month'), q('to', e['to'], 'month'), q('incomeChange', e['incomeChange']), q('expenseChange', e['expenseChange']), q('netChange', e['netChange'])])
            if e.get('before') and e.get('after'):
                add(f"上述期间已记总支出从 {money(e['before']['expenseTotal'])} 元变为 {money(e['after']['expenseTotal'])} 元。",
                    values=[q('前期总支出', e['before']['expenseTotal']), q('后期总支出', e['after']['expenseTotal'])])
            for direction, label in (('expense', '支出'), ('income', '收入')):
                for row in [r for r in e[direction + 'Drivers'] if r['change']][:3]:
                    add(f"同期{label}变化来自“{row['name']}”：{money(row['before'])} → {money(row['after'])} 元，变化 {money(row['change'])} 元。记录备注：{'、'.join(row.get('afterRemarks', []))}。备注只能说明记录内容，不能确认现实原因或是否再次发生。", True, binding={'evidenceRole': 'change_driver', 'category': row['name'], 'metric': direction}, values=[q('前期金额', row['before']), q('后期金额', row['after']), q('金额变化', row['change'])])
                    if row.get('countContribution') is not None:
                        add(f"“{row['name']}”的账单笔数由 {row['beforeCount']} 笔变为 {row['afterCount']} 笔，每笔均额由 {money(row['meanAmountBefore'])} 元变为 {money(row['meanAmountAfter'])} 元；对称分解中笔数变化贡献 {money(row['countContribution'])} 元、每笔均额变化贡献 {money(row['meanContribution'])} 元。两项相加为上面的金额变化；这是记账层面的分解，不能推断商品单价或实际购买数量变化。",
                            values=[q('前期笔数', row['beforeCount'], '笔'), q('后期笔数', row['afterCount'], '笔')] +
                                [q(name, row[name]) for name in ('meanAmountBefore', 'meanAmountAfter', 'countContribution', 'meanContribution')])
        elif tool == 'recurring':
            for direction, label in (('income', '收入'), ('expense', '支出')):
                for row in e.get(direction, [])[:4]:
                    add(f"重复出现的{label}“{row['source']}”：有记录月份 {row['activeMonths']} 个，合计 {row['transactions']} 笔，完整月份的月度中位数 {money(row['typicalAmount'])} 元。重复出现不保证未来继续发生。", values=[q('activeMonths', row['activeMonths'], '个月'), q('有记录月份数', row['activeMonths'], '个'), q('transactions', row['transactions'], '笔'), q('typicalAmount', row['typicalAmount'])])
        elif tool in {'anomalies', 'data_quality'}:
            add(f"检测到疑似重复组 {len(e['possibleDuplicates'])} 组；同类样本不足的类别包括：{'、'.join(row['category'] for row in e['insufficientCategories']) or '无'}。未命中规则不能证明账本完整或无误。", True, values=[q('疑似重复组数', len(e['possibleDuplicates']), '组')])
            for row in e['possibleDuplicates'][:3]:
                add('待核查重复线索：' + literal(json.dumps(row, ensure_ascii=False)))
            if tool == 'anomalies':
                if e.get('largeExpenses') == []:
                    add('本次同类金额规则未检出待核查大额支出；样本不足的类别无法据此判断，规则未命中也不证明消费正常或账单完整。', True)
                for row in e['largeExpenses'][:3]:
                    add('待核查大额消费：' + literal(json.dumps(row, ensure_ascii=False)))
            else:
                add(f"按备注识别的退款、转入、出售等非持续性入账共 {e['nonrecurringIncomeCount']} 笔，合计 {money(e['nonrecurringIncomeTotal'])} 元；这些线索需核查，不能直接计作持续收入或断定记账错误。", True, values=[q('nonrecurringIncomeCount', e['nonrecurringIncomeCount'], '笔'), q('nonrecurringIncomeTotal', e['nonrecurringIncomeTotal'])])
                add(f"缺少支付方式 {e['missingPaymentMethodCount']} 笔，缺少分类 {e['missingCategoryCount']} 笔。缺失字段可能影响账户或类别分析，不必然影响金额加总。", values=[q('missingPaymentMethodCount', e['missingPaymentMethodCount'], '笔'), q('missingCategoryCount', e['missingCategoryCount'], '笔')])
        elif tool == 'forecast':
            label = {'income': '收入', 'expense': '支出', 'net': '结余'}[e['metric']]
            add(f"假设历史模式延续，{e['targetMonth']} 的月度{label}基准 {money(e['estimate'])} 元，历史情景范围 {money(e['lower'])}～{money(e['upper'])} 元。基准参考月份：{'、'.join(e['baselineMonths'])}。这是条件估计，历史范围不是保证或置信区间。", True, values=[q('预测月份', e['targetMonth'], 'month'), q('预测基准', e['estimate']), q('历史情景下界', e['lower']), q('历史情景上界', e['upper'])])
            if e.get('method'):
                observed = '历史观察月份：' + '、'.join(e['observedMonths']) + '。' if e.get('observedMonths') else ''
                add('计算方法：' + e['method'] + observed, binding={'evidenceRole': 'forecast_method'},
                    values=[q('历史观察月份', month, 'month') for month in e.get('observedMonths', [])])
            for row in e.get('sourceForecasts', [])[:8]:
                historical = f"历史样本入账 {money(row['amount'])} 元，" if row.get('amount') is not None else ''
                summary = f"{row.get('member', '归属未知')}的“{row['source']}”{historical}条件估计 {money(row.get('estimate'))} 元。备注依据："
                exclusion = '此项为已剔除的入账，不计入持续收入。' if row.get('excluded') else ''
                short_notes = [str(remark) for remark in row.get('remarks', [])[:4]]
                note_preview = '；'.join(short_notes) if sum(map(len, short_notes)) <= 80 else ''
                if note_preview and len(row.get('remarks', [])) > 4:
                    note_preview += '；这里只展示备注样例，完整映射见数据依据。'
                add(summary + '；'.join(row.get('remarks', [])) + '。' + exclusion,
                    binding={'metric': 'income', 'evidenceRole': 'forecast_source', 'excluded': bool(row.get('excluded')), **{key: row[key] for key in ('member', 'memberKey', 'source', 'sourceKey') if key in row}},
                    model_text=(summary + note_preview + '。' if note_preview else summary.removesuffix('备注依据：')) + exclusion,
                    values=[q('该来源预测贡献', row.get('estimate')), q('该来源历史样本入账', row.get('amount'))])
            if e.get('excludedIncomeTotal'):
                add(f"历史样本中按备注识别并剔除非持续性入账 {money(e['excludedIncomeTotal'])} 元；归类仍需核查。", binding={'evidenceRole': 'forecast_exclusions'}, values=[q('excludedIncomeTotal', e['excludedIncomeTotal'])])
        elif tool == 'scenario':
            add(e['computedConclusion'], True)
        elif tool == 'savings':
            add(f"用户给定起始余额 {money(e['startingBalance'])} 元，目标 {money(e['targetAmount'])} 元，仍需新增 {money(e['remainingToTarget'])} 元；历史条件下月结余基准 {money(e['baselineMonthlySaving'])} 元。", True, values=[q('用户提供的可动用余额', e['startingBalance']), q('用户目标', e['targetAmount']), q('remainingToTarget', e['remainingToTarget']), q('baselineMonthlySaving', e['baselineMonthlySaving'])])
            add(e['computedConclusion'], True)
            if e['estimatedMonthsToTarget'] == 0:
                add('按用户给定余额，已经达到目标。', True)
            elif e['estimatedMonthsToTarget'] is not None:
                add(f"在当前情景月结余持续不变的条件下，约需 {e['estimatedMonthsToTarget']} 个月达到目标；未来变化可能延长或缩短期限。", True, values=[q('estimatedMonthsToTarget', e['estimatedMonthsToTarget'], '个月')])
            else:
                add('当前情景没有正的月度结余，无法按这一基准推算有限的达标期限，需要调整收支或目标。', True)
            if e.get('requiredMonthlySaving') is not None:
                add(f"按 {e['months']} 个月期限，目标所需月结余 {money(e['requiredMonthlySaving'])} 元。", True, values=[q('months', e['months'], '个月'), q('requiredMonthlySaving', e['requiredMonthlySaving'])])
        elif tool == 'budget':
            if e.get('hasRecords') is False:
                add(f"{e['month']} 尚未见账单，实际支出和预算余量未知，不能把未记录当作零支出或预算全部可用。", True,
                    values=[q('月份', e['month'], 'month'), q('budgetAmount', e['budgetAmount'])])
                continue
            add(f"{e['month']} 用户预算 {money(e['budgetAmount'])} 元，已记支出 {money(e['spent'])} 元，剩余 {money(e['remaining'])} 元。按含今天的剩余 {e['remainingDaysIncludingToday']} 天，日均可用额度 {money(e['dailyAllowance'])} 元。未记录支出会改变结果。", True, values=[q('月份', e['month'], 'month'), q('budgetAmount', e['budgetAmount']), q('spent', e['spent']), q('remaining', e['remaining']), q('remainingDaysIncludingToday', e['remainingDaysIncludingToday'], '天'), q('dailyAllowance', e['dailyAllowance'])])
        elif tool == 'projection':
            add(f"假设收支模式及所列变化延续，{e['startMonth']} 至 {e['endMonth']} 共 {e['months']} 个月：月收入 {money(e['monthlyIncome'])} 元，月支出 {money(e['monthlyExpense'])} 元，月结余 {money(e['monthlyNet'])} 元；累计新增结余 {money(e['totalNet'])} 元。", True, values=[q('startMonth', e['startMonth'], 'month'), q('endMonth', e['endMonth'], 'month'), q('months', e['months'], '个月'), q('monthlyIncome', e['monthlyIncome']), q('monthlyExpense', e['monthlyExpense']), q('monthlyNet', e['monthlyNet']), q('totalNet', e['totalNet'])])
            add(f"历史高低值延续的累计结余情景为 {money(e['lowerTotalNet'])}～{money(e['upperTotalNet'])} 元；不是统计置信区间，也不是未来保证。新增结余不等于当前存款。", True, values=[q('lowerTotalNet', e['lowerTotalNet']), q('upperTotalNet', e['upperTotalNet'])])
            if e.get('projectedBalance') is not None:
                add(f"计入用户给定起始余额后，期末余额为 {money(e['projectedBalance'])} 元。", True, values=[q('projectedBalance', e['projectedBalance'])])
            baseline = e.get('baseline', {})
            if baseline.get('incomeSources'):
                add('用于测算的重复入账来源包括：' + '、'.join(baseline['incomeSources'][:4]) + '。这些来源未来能否持续、金额是否变化，需要确认；职业或来源名称本身不能证明稳定性。', True)
            change = baseline.get('latestCompleteChange', {})
            if change.get('sufficientData'):
                drivers = [row for row in change.get('expenseDrivers', []) if row['change']]
                if drivers:
                    add('最近完整月份的支出变化包括：' + '；'.join(f"{row['name']}变化 {money(row['change'])} 元" for row in drivers[:3]) + '。未来是否重复、是否还有其他大额安排，是测算前需要确认的变数；无法仅凭账本确定最大的未来风险。', True,
                        values=[q(row['name'] + '变化', row['change']) for row in drivers[:3]])
        elif tool == 'income_stability':
            months = e['observedMonths']
            basis = {'incomeBasis': e.get('incomeBasis'), 'months': months}
            exclusions = (f"本样本剔除备注规则识别的非持续入账 {e['excludedNonrecurringIncomeCount']} 笔、{money(e['excludedNonrecurringIncomeTotal'])} 元。"
                          if e.get('excludedNonrecurringIncomeCount') is not None else '')
            add('仅在已观察完整月份 ' + '、'.join(months) + f" 共 {len(months)} 个月中，剔除备注规则识别的非持续入账后，月收入最低 {money(e['incomeMin'])} 元、最高 {money(e['incomeMax'])} 元、中位数 {money(e['incomeMedian'])} 元。"
                + exclusions + '该口径不代表全部历史入账，未被规则识别也不证明未来持续。', True,
                binding=basis, values=[q('incomeMin', e['incomeMin']), q('incomeMax', e['incomeMax']), q('incomeMedian', e['incomeMedian']),
                    q('剔除非持续入账笔数', e.get('excludedNonrecurringIncomeCount'), '笔'), q('剔除非持续入账金额', e.get('excludedNonrecurringIncomeTotal'))] +
                [q('完整月份数', len(months), unit) for unit in ('个', '个月')] + [q('完整月份', month, 'month') for month in months])
            denominator = f"纳入收入 {money(e['includedIncomeTotal'])} 元" if e.get('includedIncomeTotal') is not None else '纳入收入'
            for row in e['sources'][:3]:
                add(f"同一完整月样本剔除已识别非持续入账后，收入来源“{row['source']}”累计 {money(row['amount'])} 元，占{denominator}的 {row['sharePercent']}%。来源集中意味着该来源变化需要重点测算，并不证明它将减少。", True,
                    binding=basis, values=[q('金额', row['amount']), q('sharePercent', row['sharePercent'], '%'), q('同口径纳入收入', e.get('includedIncomeTotal'))])
        elif tool == 'optimization':
            add('在保护 ' + '、'.join(dict.fromkeys(e['protectedCategories'] + e['excludedCategories'])) + f" 等支出的前提下，本次已记录数据中可逐项核查的月度支出上界为 {money(e['reviewableMonthlySpend'])} 元；不代表都可取消，也不能证明现实中没有其他可优化支出。", True, values=[q('可核查月度候选开销', e['reviewableMonthlySpend'])])
            if not e['candidates']:
                add('下一步先核查未分类、用途不清的支出，确认是否有保护项之外可调整的项目；如仍需提高结余，再比较可实现的增收与调整目标期限。未经确认的增收不计入基准，不强行削减保护项。', True)
            if e.get('goalMonthlyRequirement') is not None:
                add(f"目标所需月结余 {money(e['goalMonthlyRequirement'])} 元，当前情景月结余 {money(e['baselineMonthlySaving'])} 元，真正需要额外改善的是 {money(e['monthlyImprovement'])} 元；不能把目标月结余当成需要额外节省的钱。", True, values=[q('goalMonthlyRequirement', e['goalMonthlyRequirement']), q('baselineMonthlySaving', e['baselineMonthlySaving']), q('monthlyImprovement', e['monthlyImprovement'])])
            if e.get('monthlyImprovement') is not None:
                if e['monthlyImprovement'] > 0:
                    add(f"每月额外改善目标 {money(e['monthlyImprovement'])} 元；即使全部取消候选项，剩余缺口为 {money(e['gapBeyondReviewableSpend'])} 元。能否削减需核查用途，不能承诺不影响生活或强行压缩保护项。", True, values=[q('monthlyImprovement', e['monthlyImprovement']), q('候选项之外的改善缺口', e['gapBeyondReviewableSpend'])])
                else:
                    add('在当前假设下无需额外节支即可满足目标要求，仍应为未来变动保留余地。', True)
            for row in e['candidates'][:4]:
                add(f"可优先核查“{row['category']}”，最近有记录完整月份的月度中位数 {money(row['typicalMonthlySpend'])} 元；需逐笔确认用途和能否削减。", values=[q('typicalMonthlySpend', row['typicalMonthlySpend'])])
        elif tool == 'purchase':
            add(f"用户给定可用余额 {money(e['startingBalance'])} 元，现金购买 {money(e['purchaseAmount'])} 元后剩 {money(e['cashAfterPurchase'])} 元。", True, values=[q('用户提供的可动用余额', e['startingBalance']), q('购买金额', e['purchaseAmount']), q('cashAfterPurchase', e['cashAfterPurchase'])])
            if e.get('reserveMargin') is not None:
                add(f"相对用户希望保留的 {money(e['reserveAmount'])} 元储备，购买后余量 {money(e['reserveMargin'])} 元。还需核对近期必付账单与收入到账时间。", True, values=[q('储备底线', e['reserveAmount']), q('reserveMargin', e['reserveMargin'])])
                if e['reserveMargin'] >= 0:
                    add('按给定余额，可以同时覆盖购买价和所需储备，不存在这两项的资金缺口；这不等于已覆盖其他尚未提供的必付账单或未来风险。', True)
                else:
                    add('按给定余额，购买后不能同时满足所需储备，需调整购买金额、时间或资金安排；未确认的未来收入不能算作现有现金。', True)
        elif tool == 'runway':
            add(f"在用户假设下，每月现金缺口 {money(e['monthlyDeficit'])} 元。" +
                (f"给定可用余额 {money(e['startingBalance'])} 元，可覆盖约 {e['runwayMonths']} 个月。" if e['runwayMonths'] is not None else '此情景没有月度缺口，不能解释为资金永远足够。'), True, values=[q('monthlyDeficit', e['monthlyDeficit']), q('runwayMonths', e['runwayMonths'], '个月'), q('用户提供的可动用余额', e['startingBalance'])])
        for change in e.get('assumptions', e.get('baseline', {}).get('assumptions', [])):
            label = '收入' if change['metric'] == 'income' else '支出'
            change_text = f"{change['percent']}%" if change.get('percent') is not None else f"每次 {money(change.get('amount'))} 元，共 {change.get('count', 1)} 次"
            add(f"用户假设：“{change.get('source') or '全部' }”{label}变化 {change_text}，该来源基准 {money(change['sourceBaseline'])} 元，月度变化 {money(change['totalChange'])} 元。", True,
                values=[q('该来源基准', change['sourceBaseline']), q('该来源变动', change['totalChange']),
                        q('该来源变动金额幅度', abs(change['totalChange'])), q('该来源变化比例', change.get('percent'), '%'),
                        q('该来源变化比例幅度', abs(change['percent']) if change.get('percent') is not None else None, '%'),
                        q('每次变动金额', change.get('amount')), q('变化次数', change.get('count', 1), '次')])
    # Deduplicate repeated tool constraints without changing scope labels.
    seen = set()
    facts = [row for row in facts if not (row['text'] in seen or seen.add(row['text']))]
    return facts




class EvidenceReporter:
    async def _invoke(self, schema, system, payload):
        if schema is not ReportDraft:
            raise ValueError('报告模型只允许生成有引用的结论')
        started = perf_counter()
        self._last_call = {'schema': schema.__name__}
        settings = get_settings()
        generation_schema = answer_schema(payload)
        if settings.llm_provider == 'openai_compatible':
            system += '\n必须遵守的 JSON schema：' + json.dumps(generation_schema, ensure_ascii=False, separators=(',', ':'))
        model = get_structured_model(schema, reasoning=False, num_predict=REPORT_TOKENS)
        if settings.llm_provider == 'ollama':
            model = model.bind(format=generation_schema)
        elif settings.llm_provider == 'openai':
            model = model.bind(response_format=response_format('LedgerConclusion', generation_schema))
        response = await asyncio.wait_for(model.ainvoke([
            ('system', system),
            ('human', json.dumps(payload, ensure_ascii=False, separators=(',', ':')))]),
            timeout=settings.planner_timeout_seconds)
        self._last_call.update(durationMs=round((perf_counter() - started) * 1000),
            promptTokens=response.response_metadata.get('prompt_eval_count'),
            outputTokens=response.response_metadata.get('eval_count'), doneReason=response.response_metadata.get('done_reason'))
        content = re.sub(r'<think>.*?</think>', '', str(response.content), flags=re.S)
        if '{' not in content or '}' not in content:
            raise ValueError(f'{schema.__name__} 未返回 JSON 对象；contentLength={len(content)}')
        return schema.model_validate_json(content[content.find('{'):content.rfind('}') + 1])

    async def _draft(self, payload):
        self._last_composition_calls = []
        self._last_composition_plan = None

        async def invoke(schema, system, scoped):
            try:
                result = await self._invoke(schema, system, scoped)
                result = schema.model_validate(result)
                self._last_composition_plan = result.model_dump()
                return result
            finally:
                self._last_composition_calls.append({**getattr(self, '_last_call', {}), 'schema': schema.__name__})

        return await compose_report(payload, invoke)

    async def answer(self, question, context, results, *, history='', instruction='', artifacts=None, understanding=None, analysis_scope=None):
        needs = set((understanding or {}).get('evidenceNeeds', []))
        forecast_metrics = {row['evidence'].get('metric') for row in results if row['tool'] == 'forecast'}
        focused = [row for row in results if not (
            row['tool'] == 'sources' and row.get('supplemental') and (forecast_metrics or (understanding and not needs & {'sources', 'members', 'remarks'})))]
        if len(forecast_metrics) == 1 and forecast_metrics <= {'income', 'expense'}:
            metric = next(iter(forecast_metrics))
            forecast_modes = {row.get('mode') for row in results if row['tool'] == 'forecast'}
            focused = [{**row, 'evidence': {**row['evidence'], 'expense' if metric == 'income' else 'income': []}}
                       if row['tool'] == 'recurring' and row.get('mode') in forecast_modes
                       and (row.get('supplemental') or row.get('metric') == metric) else row for row in focused]
        facts = build_facts(focused)
        findings = build_analysis_findings(focused, context)
        facts = [row for row in facts if row['required']] + [row for row in facts if not row['required']]
        artifacts = artifacts or []
        preview = [{**row, 'rows': row['rows'][:12], 'totalRows': len(row['rows'])} for row in artifacts[:8]]
        # Old assistant values are never authorities for the fresh calculation.
        report_history = re.sub(r'[-+]?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:元|块|%|％)',
                                '[历史数量以本轮证据为准]', history[-3200:])
        add_savings_deadline_facts(facts, focused, context, analysis_scope)
        conditions = {key: value for key, value in context.items()
                      if key not in {'conversation', 'activeAnalyses', 'activeHistoryWindow', 'scope', 'savingsDeadlineReference'}}
        payload = {'question': question, 'history': report_history, 'instruction': instruction,
                   'understanding': understanding or {}, 'userConditions': conditions,
                   'artifacts': preview, 'analysisScope': analysis_scope or {}}
        previous = context.get('previousMonths')
        projection = next((row['evidence'] for row in focused if row['tool'] == 'projection'
                           and row['evidence'].get('sufficientData')), None)
        if previous and projection:
            extra = (projection['months'] - previous) * projection['monthlyNet']
            binding = next((row['binding'] for row in facts if row.get('binding', {}).get('tool') == 'projection'), None)
            if binding:
                facts.append({'id': 'extension', 'required': True, 'binding': {**binding, 'headline': True},
                    'text': f"期限由 {previous} 个月延长至 {projection['months']} 个月，在同一假设下累计新增结余增加 {money(extra)} 元，月度结余仍是 {money(projection['monthlyNet'])} 元。"
                    + ('未给出明确存款目标，尚不能计算所需月结余能降低多少。' if context.get('targetAmount') is None else ''),
                    'values': [q('原期限', previous, '个月'), q('新期限', projection['months'], '个月'),
                               q('新增结余增量', extra), q('月度结余', projection['monthlyNet'])]})
        attention = build_attention_points(question, focused, facts, findings)
        payload.update(facts=facts, analysisFindings=findings, reviewPriorities=attention)
        report_context = audit_report_context(payload)
        attempts, calls, status, report = [], [], 'planned', None
        try:
            report_context = prepare_report_context(payload, REPORT_PROMPT, ReportDraft,
                num_ctx=get_settings().ollama_num_ctx, num_predict=REPORT_TOKENS)
            for turn in range(2):
                try:
                    draft = await self._draft(report_context)
                finally:
                    calls.extend(getattr(self, '_last_composition_calls', []))
                issues = validate_draft(draft, report_context)
                if not issues:
                    reply = render_report(draft, report_context)
                    report = draft.model_dump()
                    break
                attempts.append({'issues': issues, 'stage': 'reference_validation'})
                if turn:
                    raise ValueError('；'.join(issues))
                report_context.payload = {**report_context.payload, 'corrections': issues}
            else:
                raise ValueError('结论生成未完成')
        except Exception as exc:
            detail = '; '.join(f"{err['loc']}: {err['msg']}" for err in exc.errors()) if isinstance(exc, ValidationError) else (str(exc) if isinstance(exc, ValueError) else '')
            attempts.append({'issues': ['结论生成失败：' + type(exc).__name__ + ' ' + detail[:500]],
                             'contextBudget': report_context.budget})
            reply, status = fallback_report(report_context), 'fallback'
        return reply, {
            'status': 'grounded', 'mechanism': 'cited_conclusions', 'compositionStatus': status,
            'facts': facts, 'composerFactIds': [f['id'] for f in report_context.selected],
            'valueReferences': report_context.values, 'judgments': report_context.judgments,
            'findings': findings, 'report': report, 'attempts': attempts,
            'attentionPoints': attention,
            'compositionPlan': getattr(self, '_last_composition_plan', None),
            'calls': calls or list(getattr(self, '_last_composition_calls', [])),
            'contextBudget': report_context.budget}
