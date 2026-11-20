import unittest
from app.agent.evidence_report import build_facts
from app.agent.analysis_findings import build_analysis_findings
from app.agent.report_evidence import prepare_report_context
from app.agent.report_composition import REPORT_PROMPT,validate_draft
from app.schemas.report import ReportDraft


def results():
    drivers=[]
    for name,before,after in [('学习',9200,0),('餐饮',900,0),('租住',700,0),('交通',0,600)]:
        drivers.append({'name':name,'before':before,'after':after,'change':after-before,'afterRemarks':[name+'记录']})
    return [{'tool':'changes','mode':'个人版','metric':'all','requestedMetric':'expense','evidence':{
        'sufficientData':True,'from':'2026-09','to':'2026-10','incomeChange':2000,'expenseChange':-10200,'netChange':12200,
        'before':{'incomeTotal':5000,'expenseTotal':14500},'after':{'incomeTotal':7000,'expenseTotal':4300},
        'incomeDrivers':[{'name':'工资','before':5000,'after':7000,'change':2000}], 'expenseDrivers':drivers}}]


class RequestedComparisonDeliveryTests(unittest.TestCase):
    def data(self):
        evidence=results();facts=build_facts(evidence)
        return prepare_report_context({'question':'对比两个月支出，说明主要增加和减少，给出每月支出总额。',
            'facts':facts,'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)

    def test_largest_increase_is_retained_beyond_the_first_negative_drivers(self):
        facts=build_facts(results())
        self.assertTrue(any(f['binding'].get('category')=='交通' for f in facts))

    def test_requested_totals_and_both_change_directions_reach_the_model(self):
        data=self.data();provided={f['id'] for f in data.selected}
        totals=next(f for f in data.facts if any(v['label']=='前期总支出' for v in f.get('values',[])))
        increase=next(f for f in data.facts if f['binding'].get('category')=='交通')
        self.assertTrue({totals['id'],increase['id']} <= provided)
        self.assertFalse(any(f['binding'].get('tool')=='changes' and f['binding'].get('category')=='工资' for f in data.facts))
        fields=data.payload['answerRequirements']['requiredValues']
        self.assertEqual({v['label'] for v in fields},{'前期总支出','后期总支出'})

    def test_conclusion_cannot_omit_explicitly_requested_totals_and_increase(self):
        data=self.data();negative=next(f for f in data.selected if f['binding'].get('category')=='学习')
        draft=ReportDraft(conclusion={'text':'支出明显下降，主要由学习支出减少带动。','factIds':[negative['id']]})
        issues=validate_draft(draft,data)
        self.assertTrue(any('总额' in issue for issue in issues))
        self.assertTrue(any('增加' in issue for issue in issues))

    def test_complete_short_answer_can_use_signed_or_absolute_change(self):
        data=self.data()
        refs={}
        for v in data.values:
            refs[(v['factId'],v['label'])]='{{'+v['id']+'}}'
        totals=next(f for f in data.selected if f['binding'].get('evidenceRole')=='period_total')
        negative=next(f for f in data.selected if f['binding'].get('category')=='学习')
        positive=next(f for f in data.selected if f['binding'].get('category')=='交通')
        report=ReportDraft(conclusion={'text':'支出下降主要由学习开销减少带动。','factIds':[negative['id']]},reasons=[
            {'text':'前期总支出'+refs[totals['id'],'前期总支出']+'，后期总支出'+refs[totals['id'],'后期总支出']+'。','factIds':[totals['id']]},
            {'text':'学习减少'+refs[negative['id'],'金额变化（绝对值）']+'，交通增加'+refs[positive['id'],'金额变化']+'。','factIds':[negative['id'],positive['id']]}])
        self.assertEqual(validate_draft(report,data),[])

    def test_normal_conclusion_does_not_require_a_data_dump(self):
        evidence=results()
        data=prepare_report_context({'question':'支出变化说明什么，直接给我结论。','facts':build_facts(evidence),
            'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)
        req=data.payload['answerRequirements']
        self.assertEqual(req['requiredValues'],[])
        self.assertEqual(req['changeDirections'],[])

    def test_combined_table_request_does_not_force_each_month_totals(self):
        evidence=results()
        data=prepare_report_context({'question':'按类别给这两个月合计的支出金额、笔数和均额，请用表格展示。',
            'facts':build_facts(evidence),'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)
        self.assertEqual(data.payload['answerRequirements']['requiredValues'],[])

    def test_income_comparison_does_not_include_expense_drivers(self):
        evidence=results();evidence[0]['requestedMetric']='income'
        facts=build_facts(evidence)
        self.assertFalse(any(f['binding'].get('evidenceRole')=='change_driver' and f['binding'].get('metric')=='expense' for f in facts))
        data=prepare_report_context({'question':'各月收入总额和主要增减是什么？','facts':facts,
            'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)
        self.assertEqual({v['label'] for v in data.payload['answerRequirements']['requiredValues']},{'前期总收入','后期总收入'})

    def test_previous_remark_reaches_composer_when_an_expense_disappears(self):
        evidence=results();evidence[0]['evidence']['expenseDrivers'][0].update(beforeRemarks=['学费'],afterRemarks=[])
        facts=build_facts(evidence)
        driver=next(f for f in facts if f['binding'].get('category')=='学习')
        self.assertIn('学费',driver.get('modelText',driver['text']))
        self.assertIn('前期',driver['text'])

    def test_explicit_no_totals_request_is_respected(self):
        evidence=results()
        data=prepare_report_context({'question':'不用列每月支出总额，只给一句话结论。','facts':build_facts(evidence),
            'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)
        self.assertEqual(data.payload['answerRequirements']['requiredValues'],[])

    def test_month_totals_are_required_from_real_trends_and_aggregate_results(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        rows=[{'year':2026,'month':m,'day':1,'money':amount,'costType':'餐饮'} for m,amount in [(9,-100),(10,-200)]]
        spans=[{'label':'九月','startDate':'2026-09-01','endDate':'2026-09-30'},
               {'label':'十月','startDate':'2026-10-01','endDate':'2026-10-31'}]
        for tool in ('trends','aggregate'):
            evidence=[{'tool':tool,'mode':'个人版','metric':'all' if tool=='trends' else 'expense',
                'evidence':run_analysis(tool,rows,as_of=date(2026,11,20),metric='expense',aggregate={'periods':spans,'groupBy':['month']})}]
            data=prepare_report_context({'question':'给出各月支出总额。','facts':build_facts(evidence),
                'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)
            required=data.payload['answerRequirements']['requiredValues']
            self.assertEqual(len(required),2,tool)
            report=ReportDraft(conclusion={'text':'支出上升。','factIds':[data.selected[0]['id']]})
            self.assertTrue(any('总额' in issue for issue in validate_draft(report,data)),tool)

    def test_requested_month_table_can_deliver_totals_without_repeating_them_in_prose(self):
        evidence=results()
        data=prepare_report_context({'question':'把每月支出总额放进表格，正文只给一句话总结，不重复表中数字。',
            'artifacts':[{'kind':'table','presentation':'requested','columns':[{'key':'month'},{'key':'expense'}],
                'rows':[{'month':'2026-09','expense':14500},{'month':'2026-10','expense':4300}]}],
            'facts':build_facts(evidence),'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)
        self.assertEqual(data.payload['answerRequirements']['requiredValues'],[])

    def test_aggregate_comparison_also_retains_both_requested_change_directions(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        rows=[{'year':2026,'month':m,'day':1,'money':-amount,'costType':category}
              for category,before,after in [('学习',9200,1),('餐饮',900,1),('租住',700,1),('娱乐',500,1),('交通',1,301)]
              for m,amount in [(9,before),(10,after)]]
        spec={'periods':[{'label':'九月','startDate':'2026-09-01','endDate':'2026-09-30'},
                         {'label':'十月','startDate':'2026-10-01','endDate':'2026-10-31'}],
              'groupBy':['category']}
        evidence=[{'tool':'aggregate','mode':'个人版','metric':'expense',
            'evidence':run_analysis('aggregate',rows,as_of=date(2026,11,20),metric='expense',aggregate=spec)}]
        data=prepare_report_context({'question':'支出主要增加和减少在哪里？','facts':build_facts(evidence),
            'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)
        self.assertEqual({g['direction'] for g in data.payload['answerRequirements']['changeDirections']},{'增加','减少'})
        increase=next(f for f in data.selected if f['binding'].get('category')=='交通' and f['binding'].get('evidenceRole')=='change_driver')
        self.assertIn(increase['id'],{g['factId'] for g in data.payload['answerRequirements']['changeDirections']})

    def test_recent_comparison_does_not_hide_earlier_requested_trend_months(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        rows=[{'year':2026,'month':m,'day':1,'money':-100*m,'costType':'餐饮'} for m in range(5,11)]
        evidence=[]
        for tool in ('changes','trends'):
            evidence.append({'tool':tool,'mode':'个人版','metric':'all','requestedMetric':'expense',
                'historyWindow':{'startDate':'2026-05-01','endDate':'2026-10-31'},
                'evidence':run_analysis(tool,rows,as_of=date(2026,11,20))})
        data=prepare_report_context({'question':'给出各月支出总额。','facts':build_facts(evidence),
            'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)
        required=data.payload['answerRequirements']['requiredValues']
        lookup={v['id']:v for v in data.values}
        self.assertEqual(len(required),6)
        self.assertEqual({lookup[v['id']]['binding']['month'] for v in required},{f'2026-{m:02}' for m in range(5,11)})

    def test_large_income_comparisons_do_not_displace_expense_changes(self):
        from datetime import date
        from app.tools.financial_analysis import run_analysis
        rows=[{'year':2026,'month':m,'day':1,'money':amount,'costType':category,'orderRemark':category}
              for category,before,after in [('项目甲',1000,60000),('项目乙',1000,50000),('餐饮',-100,-50),('交通',-20,-40)]
              for m,amount in [(9,before),(10,after)]]
        spec={'periods':[{'label':'九月','startDate':'2026-09-01','endDate':'2026-09-30'},
                         {'label':'十月','startDate':'2026-10-01','endDate':'2026-10-31'}],'groupBy':['category']}
        evidence=[{'tool':'aggregate','mode':'个人版','metric':'all',
            'evidence':run_analysis('aggregate',rows,as_of=date(2026,11,20),metric='all',aggregate=spec)}]
        data=prepare_report_context({'question':'支出主要增加和减少在哪里？','facts':build_facts(evidence),
            'analysisFindings':build_analysis_findings(evidence)},REPORT_PROMPT,ReportDraft)
        self.assertEqual({g['direction'] for g in data.payload['answerRequirements']['changeDirections']},{'增加','减少'})
