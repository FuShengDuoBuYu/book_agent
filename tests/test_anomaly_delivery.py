import unittest
from datetime import date
from app.agent.analysis_findings import build_analysis_findings
from app.agent.evidence_report import build_facts
from app.tools.financial_analysis import run_analysis


class AnomalyDeliveryTests(unittest.TestCase):
    def test_unhit_large_expense_rule_is_delivered_separately_from_insufficient_sample_and_duplicates(self):
        records=[{'year':2026,'month':10,'day':d,'money':-45,'costType':'资料','orderRemark':'样本收据','bankName':'现金','clock':'10:00'} for d in [2,3,4,4]]
        records.append({'year':2026,'month':10,'day':9,'money':-90,'costType':'修理','orderRemark':'维修'})
        evidence=run_analysis('anomalies',records,as_of=date(2026,11,19))
        self.assertEqual(evidence['largeExpenses'],[])
        self.assertEqual(len(evidence['possibleDuplicates']),1)
        result={'tool':'anomalies','mode':'个人版','evidence':evidence}
        facts=build_facts([result])
        self.assertTrue(any(f.get('required') and '未检出' in f['text'] and '大额' in f['text'] for f in facts))
        finding=build_analysis_findings([result])['g1']
        self.assertTrue(any('大额' in text and '未检出' in text for text in finding['observations']))
        self.assertTrue(any('修理' in text and '样本不足' in text for text in finding['limitations']))
        text=''.join(finding['observations']+finding['limitations'])
        self.assertNotIn('没有异常消费',text)
        self.assertIn('疑似重复',text)

    def test_unknown_anomaly_result_does_not_become_negative_detection(self):
        finding=build_analysis_findings([{'tool':'anomalies','evidence':{}}])['g1']
        self.assertFalse(any('未检出' in text and '大额' in text for text in finding['observations']))
