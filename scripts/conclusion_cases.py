"""Fictional report snapshots and semantic gold, fixed before model evaluation."""

from datetime import date

from scripts.evaluate_analysis import FixtureLedger
from app.tools.financial_analysis import run_analysis, shift_month


def conclusion_cases(today):
    records = FixtureLedger().orders
    start = shift_month(today, -5).replace(day=1)
    rows = [r for r in records if start <= date(r['year'], r['month'], r['day']) <= today]
    next_month = shift_month(today, 1).strftime('%Y-%m')
    scope = {'asOfDate': today.isoformat(), 'historyWindow': {'startDate': start.isoformat(), 'endDate': today.isoformat()}}
    def result(tool, source=None, **kwargs):
        source = rows if source is None else source
        return {'tool': tool, 'mode': '个人版', 'historyWindow': scope['historyWindow'],
                'queryIds': ['fixture'], 'evidence': run_analysis(tool, source, as_of=today,
                    window_start=start, window_end=today, **kwargs)}
    overview = [result(t) for t in ('cashflow', 'trends', 'changes', 'income_stability', 'data_quality')]
    forecast = [result('forecast', metric='income', target_month=next_month), {**result('sources'), 'supplemental': True}]
    extra = rows + [{'id': 1000 + month, 'year': today.year, 'month': month, 'day': 7,
                     'money': -300, 'costType': '购物', 'orderRemark': '非必需购物'} for month in range(start.month, today.month)]
    specifications = [
      ('overview', '分析最近半年的收支变化和主要原因。', overview, {}, None,
       ['首句解释整体变化意味着什么，不能仅报合计', '指出最近完整月增加主要来自医疗900元', '不能把单次记录认定为持续上涨或生活失控', '未完成月份不能直接比较']),
      ('priorities', '综合看最值得注意的三个问题是什么？', overview, {}, None,
       ['给出有证据支持的三个关注点', '区别医疗变动、收入集中与字段/样本核查', '不能凭结余或来源名宣称抗风险强弱']),
      ('forecast', '你预计我下个月收入是多少？', forecast, {}, 6600,
       ['首句给出6600元条件基准', '不把历史累计入账当未来收入', '不保证到账、不展开来源目录']),
      ('forecast_basis', '为什么不能直接把历史全部入账当下个月的收入？', forecast, {}, None,
       ['解释累计口径与按月来源基准的区别', '未来延续仍需确认，不能凭名字认定稳定']),
      ('purchase_unknown', '下个月花6000元买电脑合适吗？会影响生活吗？',
       [result('purchase', purchase_amount=6000), result('cashflow')], {}, None,
       ['明确缺可动用余额，暂不能判断', '不能凭月结余判买得起或买不起', '只问关键缺项而不是重问已知价格']),
      ('purchase_short', '可动用3000元，买6000元电脑，还要留1000元，够吗？',
       [result('purchase', starting_balance=3000, purchase_amount=6000, reserve_amount=1000)], {}, None,
       ['首句资金不足，不能因未知其他账单回避已知结论', '现金与储备分别核对']),
      ('purchase_reserve', '可动用10000元，买6000元后还要留5000元应急钱，合适吗？',
       [result('purchase', starting_balance=10000, purchase_amount=6000, reserve_amount=5000)], {}, None,
       ['区分付得起与不满足储备底线', '不得说完全没问题或储备足够']),
      ('purchase_fit', '可动用12000元，买6000元后保留5000元应急钱，是否足够？',
       [result('purchase', starting_balance=12000, purchase_amount=6000, reserve_amount=5000)], {}, None,
       ['给定条件下可支付且留足储备', '买后6000元、余量1000元引用正确', '不能保证未来无影响']),
      ('savings_deadline', '现在有10000元，三个月存到20000元能实现吗？',
       [result('savings', starting_balance=10000, target_amount=20000, months=3)], {}, None,
       ['按当前收支假设可达标，预计期末22000元', '不把能力与目标要求混淆']),
      ('savings_open', '现在有10000元，保持当前收支多久能存到20000元？没有固定期限。',
       [result('savings', starting_balance=10000, target_amount=20000)], {}, 3,
       ['直接给约三个月，前提每月结余模式持续', '不能再次索取用户明确取消的期限']),
      ('optimize', '每月想多留下1000元，房租和餐饮别动，其他怎么调整？',
       [result('optimization', extra, monthly_improvement=1000, protected_categories=['房租', '餐饮'])],
       {'protectedCategories': ['房租', '餐饮']}, None,
       ['购物可核查上界300元，仍有700元改善缺口', '不建议削减保护项、不把上界当必能全省', '建议与缺口直接相关']),
      ('runway', '所有收入都停了，现有可动用10000元，支出维持原样能撑多久？',
       [result('runway', starting_balance=10000, changes=[{'metric':'income','percent':-100}])], {}, 3.85,
       ['直接给约3.85个月的条件测算', '分母月缺口2600元，不用正常月结余4000元']),
      ('quality', '账本里有哪些容易误判的地方，会影响什么结论？', [result('data_quality'), result('anomalies')], {}, None,
       ['区分字段缺失、同类样本不足与已确认错误', '没有命中不证明完整无误', '说明对分类归因/趋势判断的影响而非堆字段']),
      ('table', '按来源列个收入表，再给一句总结。', [result('sources')], {}, None,
       ['完整表格独立交付且可展开', '正文只给相关总结，不附加Markdown表格']),
      ('one_sentence', '别堆数据，一句话告诉我最近收支的主要结论。', overview[:3], {}, None,
       ['只一个简短结论段落', '含有明确综合判断而非逐月流水']),
      ('detailed_method', '详细说明你怎样推算下个月收入，以及这个估计有哪些限制。', forecast, {}, None,
       ['解释最近完整月份、按来源取中位数再汇总', '区分样本与预测', '展开的是方法和边界，不是全部原始数据']),
    ]
    return [{'case': name, 'question': question, 'results': evidence, 'context': context,
             'expectedValue': expected, 'semanticGold': gold, 'scope': scope,
             'output': {'table': name == 'table', 'metric': 'income' if name in {'forecast','forecast_basis','detailed_method','table'} else 'all'}}
            for name, question, evidence, context, expected, gold in specifications]
