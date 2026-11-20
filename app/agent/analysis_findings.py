"""Qualitative explanations for the report, derived from computed ledger evidence."""

import re

from app.agent.report_values import literal


METRICS = {'income': '收入', 'expense': '支出', 'net': '结余'}
INPUTS = {'startingBalance': '当前可动用余额', 'reserveAmount': '应急储备底线',
          'purchaseAmount': '购买价格', 'changes': '具体的收支变化假设', 'source': '变化来源的依据',
          'targetAmount': '储蓄目标', 'months': '目标期限', 'budgetAmount': '预算金额'}
CHANGE = ('下降', '持平', '上升')
SIGN = ('为负', '持平', '为正')
# Keep notes containing quantities in the evidence table, outside qualitative remark examples.
QUANTITY = re.compile(r'[%％￥¥]|[\d零〇一二三四五六七八九十百千万两]+(?:[.,，\d]*\s*)(?:元|块|万|笔|次|成|个月|年|月|日)')


def _direction(value, words):
    return words[(value > 0) - (value < 0) + 1]


def _name(value):
    text = str(value)
    return literal(text) if re.search(r'[\[\]`*_~<>\\|\n\r]|://|www\.|@|\b[a-z0-9.-]+\.[a-z]{2,}\b', text, re.I) else text


def _remark_examples(remarks):
    return [s[:160] for s in remarks if s and not QUANTITY.search(s)][:4]


def _aggregate_ranks(e, out):
    dimensions = [d for d in e.get('dimensions', []) if d != 'period']
    if not dimensions or e.get('metric') not in {*METRICS, 'all'}:
        return
    measures = METRICS if e['metric'] == 'all' else [e['metric']]
    calendar = [d for d in dimensions if d in {'month', 'year'}] if any(d not in {'month', 'year'} for d in dimensions) else []
    ranks, summaries = [], []
    for covered in e.get('coverage', []):
        period = covered['period']
        if covered.get('matchedCount') == 0:
            summaries.append(f'{_name(period)}的已查记录中没有可排名的匹配分组。' if covered.get('queryCoverage') != 'not_queried'
                             else f'{_name(period)}未查询，分组排名未知。')
            continue
        slices = {}
        for row in e.get('rows', []):
            if row.get('period', period) == period and row.get('count', 0) > 0:
                slices.setdefault(tuple(row[d] for d in calendar), []).append(row)
        visible_only = bool(e.get('omittedGroupCount')) or covered.get('queryCoverage') != 'complete'
        for dates, rows in slices.items():
            for metric in measures:
                matched = [r for r in rows if r.get(metric) is not None and
                           (metric == 'net' or r.get(metric + 'Count', 0) > 0)]
                if not matched:
                    continue
                largest = max(r[metric] for r in matched)
                winners = [r for r in matched if r[metric] == largest]
                groups = [{d: str(r[d]) for d in dimensions} for r in winners[:4]]
                tied = len(winners) > 1
                ranks.append({'period': period, 'metric': metric, 'groups': groups, 'tied': tied,
                              'tiesOmitted': len(winners) > len(groups), 'visibleOnly': visible_only})
                names = '、'.join('／'.join(map(_name, g.values())) for g in groups)
                scope = '已展示的已查分组' if visible_only else '有匹配记录的分组'
                where = '／'.join(map(_name, [period, *dates]))
                summaries.append(f'{where}的{scope}中，{names}' + ('等' if len(winners) > len(groups) else '')
                                 + f'的已记{METRICS[metric]}金额' + ('并列最高。' if tied else '最高。'))
    if ranks:
        out['labels']['leadingGroups'] = ranks[:12]
        if len(ranks) > 12:
            out['limitations'].append('排名摘要只列部分区间或指标。')
        if any(r['visibleOnly'] for r in ranks):
            out['limitations'].append('部分排名仅限已展示分组，未查或未展示的分组无法据此排序。')
    out['observations'].extend(summaries[:2])


def _change_finding(e, out, tool):
    obs = out['observations']
    totals_by_metric = {}
    if tool == 'changes':
        totals_by_metric = {m: e.get(m + 'Change') for m in METRICS}
        candidates = [(row, direction, row['name']) for direction in ('expense', 'income')
                      for row in e.get(direction + 'Drivers', []) if row.get('name')]
    else:
        _aggregate_ranks(e, out)
        dims = e.get('comparisonDimensions', [])
        candidates = [(row, row.get('metric'), '、'.join(str(row[d]) for d in dims if d in row))
                      for row in e.get('comparisons', [])]
        # Never rank changes across different period pairs as if they explained one total.
        pairs = {(row.get('beforePeriod'), row.get('afterPeriod')) for row, _, _ in candidates}
        if len(pairs) > 1:
            out['limitations'].append('本次涉及多组区间，需在同一组区间内比较变化。')
            candidates = []
        totals = e.get('periodComparisons', [])
        if len({(r.get('beforePeriod'), r.get('afterPeriod')) for r in totals}) == 1:
            totals_by_metric = {r['metric']: r.get('change') for r in totals}
        if len(totals) == 1 and totals[0].get('change') is not None:
            r = totals[0]
            obs.append(f"按所选条件，对比期整体{METRICS[r['metric']]}{_direction(r['change'], CHANGE)}。")
    for metric in ('expense', 'income', 'net'):
        rows = [(r, name) for r, m, name in candidates if m == metric and name and
                all(r.get(k) is not None for k in ('before', 'after', 'change')) and r['change']]
        rows.sort(key=lambda pair: -abs(pair[0]['change']))
        total_change = totals_by_metric.get(metric)
        if total_change:
            aligned = [(row, name) for row, name in rows if row['change'] * total_change > 0]
            if aligned:
                top_change = aligned[0][0]['change']
                leaders = out['labels'].setdefault('leadingChanges', [])
                for row, name in aligned:
                    if row['change'] != top_change or len(leaders) >= 6:
                        break
                    seen = row.get('beforeCount')
                    prior = seen > 0 if seen is not None else (True if row['before'] > 0 and metric != 'net' else None)
                    leaders.append({'metric': metric, 'label': name, 'increased': top_change > 0,
                                    'previouslyRecorded': prior,
                                    'afterRemarks': _remark_examples(row.get('afterRemarks', []))})
                obs.append(f"已列{METRICS[metric]}分组中，“{_name(aligned[0][1])}”的同方向变化金额居首。")
        for row, name in rows[:2]:
            name = _name(name)
            # A zero net is not proof of an absent category. For a directional amount, zero
            # only establishes absence of a positive amount, unless a record count is known.
            earlier = row.get('beforeCount')
            if earlier is not None:
                prior = '前期已有该项记录' if earlier else '前期未见该项记录'
            elif metric != 'net':
                prior = '前期已有该项已记金额' if row['before'] > 0 else '前期未见该方向的正金额'
            else:
                prior = '比较的是该项净额'
            obs.append(f"“{name}”{prior}，对比期已记{METRICS[metric]}{_direction(row['change'], CHANGE)}。")
            if all(row.get(k) is not None for k in ('beforeCount', 'afterCount', 'meanAmountBefore', 'meanAmountAfter')):
                obs.append(f"“{name}”的记账笔数{_direction(row['afterCount'] - earlier, CHANGE)}，每笔均额"
                           f"{_direction(row['meanAmountAfter'] - row['meanAmountBefore'], CHANGE)}。")
    if not obs and tool == 'aggregate':
        coverage = e.get('coverage', [])
        if any(p.get('observed') for p in coverage):
            obs.append('汇总覆盖查询所得的已记记录，分组与合计均按所选筛选条件计算。')
        if any(p.get('queryCoverage') != 'complete' or not p.get('observed') for p in coverage):
            out['limitations'].append('未查全或整期未见账单时，完整跨期变化未知。')
    out['limitations'].append('记账金额、笔数及每笔均额的变化不能直接证明商品单价或现实消费原因。')


def _source_finding(e, out, tool):
    forecasting = tool == 'forecast'
    rows = e.get('sourceForecasts' if forecasting else 'sources', [])
    entries = []
    for row in rows[:12]:
        entries.append({'member': row.get('member') or '归属未知', 'source': row['source'],
                        'metric': row.get('metric', 'income' if forecasting else None),
                        'excludedFromRecurringIncome': row.get('excluded'),
                        'remarks': _remark_examples(row.get('remarks', []))})
    out['labels']['sources'] = entries
    if entries:
        out['observations'].append('各条备注按所列成员、收支方向和来源对应；同一来源下的备注是该来源的归组依据。')
    if any(entry['excludedFromRecurringIncome'] is True for entry in entries):
        out['observations'].append('标为剔除的入账仍属于历史已记来源，未计入持续收入预测。')
    if forecasting and e.get('sufficientData') is True:
        out['observations'].append('观察期用于查看历史记录；预测基准取最近有账单的完整月份。')
        metric = e.get('metric')
        if metric in {'income', 'net'}:
            out['observations'].append('收入先按成员和来源取基准期月金额的中位数，再加总各来源贡献。')
        if metric in {'expense', 'net'}:
            out['observations'].append('支出取基准期月支出总额的中位数。')
        if metric == 'net':
            out['observations'].append('结余以预测收入减去预测支出得到。')
    if len(rows) > len(entries) or e.get('omittedSourceCount'):
        out['limitations'].append('当前摘要只列部分来源，未列出的来源仍可能有记录。')
    out['limitations'].append('备注是已记录的描述，不能证明工作或收入以后会延续。')


def _decision(e, out, tool, context):
    obs, states = out['observations'], out['states']
    baseline = e.get('baseline') or {}
    if tool == 'scenario':
        uses_forecast = all(baseline.get(m, {}).get('sufficientData') is True for m in ('income', 'expense'))
    elif tool == 'optimization':
        uses_forecast = e.get('goalMonthlyRequirement') is not None and e.get('baselineMonthlySaving') is not None
    else:
        uses_forecast = tool in {'savings', 'projection', 'runway'} and baseline.get('sufficientData') is True
    if uses_forecast:
        method = '月结余基准用各成员收入来源的月金额中位数之和，减去月支出总额的中位数'
        out['labels']['baselineMethod'] = method
        obs.append(method + '。')
    if tool == 'savings':
        projected, target = e.get('projectedBalance'), e.get('targetAmount')
        meets = projected >= target if projected is not None and target is not None else None
        states['meetsDeadline'] = meets
        if meets is not None:
            obs.append('按当前假设，期限内可达到储蓄目标，无需额外节省。' if meets else
                       '按当前假设，期限结束时仍有目标缺口。')
        elif e.get('months') is None and e.get('remainingToTarget') is not None:
            finite = e.get('estimatedMonthsToTarget') is not None
            states['finiteTimeToTarget'] = finite
            if e['remainingToTarget'] == 0:
                obs.append('当前余额已经达到目标。')
            elif finite:
                obs.append('未指定截止期限，若当前情景持续，可以估计达标所需时间。')
            else:
                obs.append('未指定截止期限，按当前情景无法估计有限的达标时间。')
        out['limitations'].append('期限内能否达标取决于后续净现金流是否延续。')
    elif tool == 'purchase':
        for key, state, yes, no in (
                ('cashAfterPurchase', 'coversPrice', '给定现金足以支付购买价。', '给定现金不足以支付购买价。'),
                ('reserveMargin', 'coversPriceAndReserve', '买后仍能留足给定的应急储备。', '买后余额不足以留足给定储备。')):
            value = e.get(key)
            states[state] = value >= 0 if value is not None else None
            if value is not None:
                obs.append(yes if value >= 0 else no)
        out['limitations'].append('购买测算还需结合近期必付账单及到账时间。')
    elif tool == 'runway':
        value = e.get('monthlyDeficit')
        if value is not None:
            states['hasDeficit'] = value > 0
            obs.append('月缺口按情景月支出减去情景月收入计算，只取非负值。')
            obs.append('当前情景有月度资金缺口，覆盖时长按可动用余额除以月缺口计算。' if value > 0 else
                       '当前情景没有月度缺口，不能据此认定资金可无限期维持。')
            if value > 0 and e.get('startingBalance') is not None and e['startingBalance'] < 0:
                obs.append('可动用余额为负时按无可用资金计算。')
    elif tool in {'projection', 'scenario'}:
        changes = e.get('assumptions', e.get('baseline', {}).get('assumptions', []))
        states['assumptionsApplied'] = bool(changes)
        out['labels']['assumptions'] = [{'metric': c.get('metric'), 'source': c.get('source') or '该方向全部来源',
                                         'direction': _direction(c['totalChange'], CHANGE)}
                                        for c in changes[:8] if c.get('totalChange') is not None]
        applied = [f"{_name(c['source'])}的{METRICS[c['metric']]}{c['direction']}"
                   for c in out['labels']['assumptions'] if c['metric'] in METRICS]
        if applied:
            obs.append('本次已应用的变化包括你指定的' + '、'.join(applied) + '。')
        value = e.get('totalNet' if tool == 'projection' else 'net')
        if value is not None:
            obs.append(('期间累计新增结余' if tool == 'projection' else '情景月结余') + _direction(value, SIGN) + '。')
        if tool == 'projection' and e.get('monthlyNet') is not None:
            previous, current = context.get('previousMonths'), e.get('months')
            extended = current > previous if current is not None and previous is not None else None
            states['deadlineExtended'] = extended
            if extended:
                effect = _direction(e['monthlyNet'], ('累计缺口扩大', '累计结余不变', '累计结余增加'))
                obs.append(f'相同假设下，本次延长期限后{effect}，每月结余能力保持相同。')
            if context.get('targetAmount') is None:
                out['limitations'].append('未给出储蓄目标金额，无法比较不同期限下每月需要存下多少。')
                if extended:
                    out['actions'].append('如果要比较每月的储蓄压力，先确定希望在期末留存的金额。')
            if sources := baseline.get('incomeSources'):
                out['labels']['incomeSources'] = list(dict.fromkeys(sources))[:4]
                obs.append('历史完整月份重复出现的入账来源包括' + '、'.join(map(_name, out['labels']['incomeSources'])) + '。')
            latest = baseline.get('latestCompleteChange') or {}
            if latest.get('sufficientData') is True:
                drivers = [{'category': row['name'], 'direction': _direction(row['change'], CHANGE)}
                           for row in latest.get('expenseDrivers', []) if row.get('name') and row.get('change')][:3]
                if drivers:
                    out['labels']['observedExpenseChanges'] = drivers
                    obs.append('最近完整月份已观察到' + '、'.join(_name(r['category']) + '支出' + r['direction'] for r in drivers) + '。')
                    out['actions'].append('可先核对这些历史支出变化是否对应已确定的后续安排，再决定是否单列新的支出假设。')
        out['limitations'].append('情景只在所列假设延续时成立，新增结余不能代替起始存款。')
    elif tool == 'optimization':
        out['labels']['protected'] = list(dict.fromkeys(e.get('protectedCategories', []) + e.get('excludedCategories', [])))[:20]
        out['labels']['candidates'] = [r['category'] for r in e.get('candidates', [])[:12]]
        if 'candidates' in e:
            states['hasCandidates'] = bool(e['candidates'])
        if states.get('hasCandidates') is False:
            obs.append('当前记录中未识别出保护项之外可核查的候选支出。保护项应继续保留。')
        elif 'reviewableMonthlySpend' in e:
            obs.append('保护项已从候选中排除，候选总额是可核查的月度上界。')
        improvement, gap = e.get('monthlyImprovement'), e.get('gapBeyondReviewableSpend')
        if improvement == 0:
            obs.append('给定目标在当前情景下无需额外改善结余。')
        elif gap is not None and gap > 0:
            obs.append(('当前可核查支出无法填补目标缺口。' if states.get('hasCandidates') is False else
                        '即使候选开销全部削减仍有缺口。') + '若目标和期限固定，需要其他改善途径，例如增收。')
        elif gap == 0 and improvement is not None and improvement > 0:
            obs.append('候选总额在金额上可覆盖所需改善，实际能否削减仍需逐项确认。')
        if improvement != 0 and states.get('hasCandidates') is False:
            out['actions'].append('保留受保护支出，先检查已有账单中是否还有未分类或用途不明的记录。')
        elif improvement != 0 and states.get('hasCandidates'):
            names = '、'.join(map(_name, out['labels']['candidates'][:3]))
            out['actions'].append(f'先核对{names}的具体用途，再确定实际可调整金额。')
        if gap is not None and gap > 0:
            out['actions'].append('若目标和期限不变，可再核实可实现的增收；若期限可调整，再比较延后达标的安排。')
        out['limitations'].append('可核查的上界不能当作可全部取消的支出。')


def build_analysis_findings(results, context=None):
    """Produce one bounded, nonnumeric interpretation per calculation in its original order."""
    findings = {}
    for index, result in enumerate(results, 1):
        tool, e = result['tool'], result.get('evidence') or {}
        obs, limits = [], []
        out = {'tool': tool, 'scope': result.get('mode', '当前账本'), 'observations': obs,
               'limitations': limits, 'actions': [], 'labels': {}, 'states': {'sufficientData': e.get('sufficientData')}}
        missing = [INPUTS[k] for k in e.get('missingInputs', []) if k in INPUTS]
        if missing:
            obs.append('尚缺' + '、'.join(missing) + '。')
        if tool == 'projection':
            limits.append('账本可供核查已观察的收支，却不足以按发生概率或重要性排列未来风险，不能断定哪项是最大变数。')
        if e.get('sufficientData') is False:
            limits.append('当前输入或已观察样本不足，无法确定所问结果。')
        elif tool in {'changes', 'aggregate'}:
            _change_finding(e, out, tool)
        elif tool in {'forecast', 'sources'}:
            _source_finding(e, out, tool)
        elif tool in {'savings', 'purchase', 'runway', 'optimization', 'projection', 'scenario'}:
            _decision(e, out, tool, context or {})
        elif tool == 'cashflow':
            average = e.get('completeMonthAverage', {})
            if average.get('net') is not None and average.get('months'):
                obs.append('本次现金流筛选范围内，历史完整月份的月均已记结余' + _direction(average['net'], SIGN) + '；该判断只适用于所选账单。')
            else:
                limits.append('缺少可计算月均结余的完整月份。')
            for field, label in [('incomeSources', 'source'), ('expenseCategories', 'name')]:
                names = [r[label] for r in e.get(field, [])[:3] if r.get(label)]
                if names:
                    out['labels'][field] = names
            if out['labels']:
                for key, desc in [('incomeSources', '收入来源'), ('expenseCategories', '支出类别')]:
                    if names := out['labels'].get(key):
                        obs.append(f'本次现金流取数窗口内已记金额靠前的{desc}有' + '、'.join(map(_name, names)) + '。')
                majority = next((r['source'] for r in e.get('incomeSources', []) if (r.get('sharePercent') or 0) > 50), None)
                if majority:
                    obs.append(f'{_name(majority)}占本次现金流取数窗口已记收入的大部分；本句分母为该窗口收入，跨结果比较前需核对日期与筛选。')
                    out['actions'].append(f'安排近期付款前，可先核实{_name(majority)}下次到账时间与必付账单到期日。')
                limits.append('金额排名不能证明收入会持续，也不能据此判断支出负担或可削减性。')
            limits.append('历史现金流不能说明目前可动用的余额和到期债务，单凭结余无法判断抗风险能力。')
        elif tool == 'budget':
            out['states']['hasRecords'] = e.get('hasRecords')
            if e.get('hasRecords') is True and e.get('remaining') is not None:
                remaining = e['remaining']
                out['states']['overBudget'] = remaining < 0
                obs.append('按目标月已记支出，' + ('预算已超出。' if remaining < 0 else
                           '预算尚有余量。' if remaining > 0 else '预算恰好用尽。'))
            elif e.get('hasRecords') is False:
                limits.append('目标月未见账单，实际支出与预算余量仍需核实。')
        elif tool == 'trends':
            change = e.get('latestCompleteChange') or {}
            for metric, label in METRICS.items():
                if change.get(metric + 'Change') is not None:
                    obs.append(f"最近可比完整月份的已记{label}{_direction(change[metric + 'Change'], CHANGE)}。")
            limits.append('未结束月份和整月不能直接比较；缺账月份的实际金额未知。')
        elif tool == 'recurring':
            out['labels']['repeatedSources'] = [{'source': r['source'], 'member': r.get('member'), 'metric': m}
                                               for m in ('income', 'expense') for r in e.get(m, [])[:6]]
            if out['labels']['repeatedSources']:
                obs.append('所列来源在不同月份重复出现。')
            limits.append('重复出现属于历史观察，未来是否继续仍需核实。')
        elif tool == 'income_stability':
            if e.get('incomeMin') is not None and e.get('incomeMax') is not None:
                same = e['incomeMin'] == e['incomeMax']
                out['states']['constantInSample'] = same
                obs.append('剔除备注规则识别的非持续入账后，所统计完整月份的纳入收入相同。' if same else '剔除备注规则识别的非持续入账后，所统计完整月份的纳入收入有波动。')
            majority = [r['source'] for r in e.get('sources', []) if (r.get('sharePercent') or 0) > 50]
            if majority:
                out['labels']['majoritySource'] = majority[0]
                obs.append('“' + _name(majority[0]) + '”占同一完整月样本剔除已识别非持续入账后纳入收入的大部分。')
            limits.append('该样本剔除了备注规则识别的非持续性入账，不代表全部历史入账；未被识别不证明收入持续，历史集中度和波动也不能证明未来稳定或安全。')
        elif tool in {'data_quality', 'anomalies'}:
            if 'possibleDuplicates' in e:
                obs.append('命中疑似重复线索，需要核对原始记录。' if e['possibleDuplicates'] else '本次规则未检出疑似重复。')
            if e.get('largeExpenses'):
                obs.append('同类金额规则标出了待核查的大额支出。')
            elif tool == 'anomalies' and e.get('largeExpenses') == []:
                obs.append('本次同类金额规则未检出待核查大额支出。')
            if e.get('insufficientCategories'):
                names = '、'.join(_name(r['category']) for r in e['insufficientCategories'][:4])
                limits.append(f'{names}的同类记录样本不足，暂不能据此判断金额是否异常。分类是否缺失由单独的字段检查确认。')
            if tool == 'data_quality':
                measured = {key: name for key, name in [('missingPaymentMethodCount', '支付方式'),
                            ('missingCategoryCount', '分类')] if e.get(key) is not None}
                fields = [name for key, name in measured.items() if e[key] > 0]
                out['labels'].update(checkedFields=list(measured.values()), missingFields=fields)
                if fields:
                    names = '、'.join(fields)
                    obs.append('检测到的缺失字段为' + names + '，会影响按' + names + '归集；缺失本身不能证明合计有误。')
                    out['actions'].append('优先核对账本中' + names + '空缺的记录，再按这些字段比较。')
                if measured:
                    limits.append('字段缺失检测仅涵盖' + '、'.join(measured.values()) + '。')
            if e.get('nonrecurringIncomeCount'):
                obs.append('备注中出现可能属于非持续性入账的线索，需要核查口径。')
            limits.append('检测只提供核查线索，未命中也不能证明账本完整无误。')
        if not obs and not limits:
            out['states']['dataBounded'] = True
            limits.append('现有计算不足以支持额外的定性判断。')
        out['observations'] = obs[:6]
        out['limitations'] = limits[:4]
        findings[f'g{index}'] = out
    return findings
