import unittest
from app.agent.plan_review import sanitize_user_conditions
from app.schemas.plan import AgentPlan


class ColloquialBindingTests(unittest.TestCase):
    def plan(self, tool, **conditions):
        return AgentPlan.model_validate({'intent':'analyze_orders', 'analysisType':'financial_analysis',
            'toolCalls':[{'id':'ledger','toolName':'search_personal_orders'}],
            'analyses':[{'tool':tool, **conditions}]})

    def test_colloquial_target_is_preserved_by_the_real_user_condition_guard(self):
        plan=self.plan('savings',startingBalance=10000,targetAmount=22000,months=3)
        issues=sanitize_user_conditions(plan,'我现在有一万元，想三个月存到两万二。', '个人版', {})
        self.assertEqual(plan.analyses[0].targetAmount,22000)
        self.assertEqual(plan.analyses[0].startingBalance,10000)
        self.assertFalse(any(i.get('field')=='targetAmount' for i in issues))

    def test_colloquial_balance_is_preserved_without_becoming_a_goal(self):
        plan=self.plan('purchase',startingBalance=12000,purchaseAmount=700,reserveAmount=5000)
        sanitize_user_conditions(plan,'我现在可动用一万二，拿七百元买扫描仪，买完留五千元应急金。','个人版',{})
        self.assertEqual(plan.analyses[0].startingBalance,12000)
        self.assertEqual(plan.analyses[0].purchaseAmount,700)
        self.assertEqual(plan.analyses[0].reserveAmount,5000)
        self.assertIsNone(plan.analyses[0].targetAmount)

    def test_explicit_zero_and_unsupported_amounts_are_still_distinguished(self):
        plan=self.plan('purchase',startingBalance=10002,purchaseAmount=700)
        sanitize_user_conditions(plan,'我现在可动用一万零二，拿七百元买扫描仪。','个人版',{})
        self.assertEqual(plan.analyses[0].startingBalance,10002)
        wrong=self.plan('purchase',startingBalance=120000,purchaseAmount=700)
        sanitize_user_conditions(wrong,'我现在可动用一万二，拿七百元买扫描仪。','个人版',{})
        self.assertIsNone(wrong.analyses[0].startingBalance)
