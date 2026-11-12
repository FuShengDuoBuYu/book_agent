import unittest

from app.agent.analysis_context import resolve_context
from app.schemas.plan import AgentPlan, AnalysisRequest


def plan(tool, follow=True, **kwargs):
    return AgentPlan(analysisType='financial_analysis', followUpOfPreviousQuestion=follow,
                     analyses=[AnalysisRequest(tool=tool, **kwargs)])


class AnalysisContextTests(unittest.TestCase):
    def test_baseline_and_adjusted_comparison_remain_distinct(self):
        current = AgentPlan(analyses=[AnalysisRequest(tool='projection', months=3),
            AnalysisRequest(tool='projection', months=3, changes=[{'metric': 'income', 'source': '家教', 'percent': -50}])])
        state = resolve_context(current, {})
        self.assertEqual(current.analyses[0].changes, [])
        self.assertEqual(current.analyses[1].changes[0].percent, -50)
        self.assertTrue(state['ambiguous'])

    def test_personal_and_family_balances_are_separate(self):
        current = AgentPlan.model_validate({'toolCalls': [
            {'id': 'p', 'toolName': 'search_personal_orders'}, {'id': 'f', 'toolName': 'search_family_orders'}],
            'analyses': [{'tool': 'purchase', 'queryIds': ['p'], 'startingBalance': 10000, 'purchaseAmount': 6000},
                         {'tool': 'purchase', 'queryIds': ['f'], 'purchaseAmount': 6000}]})
        resolve_context(current, {})
        self.assertIsNone(current.analyses[1].startingBalance)

    def test_implicit_personal_scope_cannot_supply_family_balance(self):
        first = plan('purchase', follow=False, startingBalance=10000, purchaseAmount=1000)
        context = resolve_context(first, {}, book_mode='个人版')
        second = plan('purchase', purchaseAmount=2000)
        resolve_context(second, context, book_mode='家庭版')
        self.assertIsNone(second.analyses[0].startingBalance)

    def test_two_additive_changes_in_one_scenario_are_preserved(self):
        current = plan('scenario', follow=False, changes=[{'metric': 'income', 'amount': 1000, 'label': '兼职'},
                                                         {'metric': 'income', 'amount': 500, 'label': '授课'}])
        state = resolve_context(current, {})
        self.assertEqual(sum(row.amount for row in current.analyses[0].changes), 1500)
        self.assertFalse(state.get('ambiguous'))

    def test_alternative_scenarios_keep_their_own_conditions(self):
        current = AgentPlan(analyses=[AnalysisRequest(tool='projection', months=3,
                    changes=[{'metric': 'income', 'source': '家教', 'percent': -50}]),
                AnalysisRequest(tool='projection', months=5,
                    changes=[{'metric': 'income', 'source': '家教', 'percent': -20}])])
        state = resolve_context(current, {})
        self.assertEqual([t.months for t in current.analyses], [3, 5])
        self.assertEqual([t.changes[0].percent for t in current.analyses], [-50, -20])
        self.assertTrue(state['ambiguous'])

    def test_four_turn_chain_preserves_source_constraints_and_extends_horizon(self):
        first = plan('projection', follow=False, months=3)
        context = resolve_context(first, {})
        second = plan('projection', changes=[{'metric': 'income', 'percent': -50, 'source': '家教'}])
        context = resolve_context(second, context)
        self.assertEqual(second.analyses[0].months, 3)
        third = plan('optimization', protectedCategories=['房租', '餐饮'])
        context = resolve_context(third, context)
        self.assertEqual(third.analyses[0].changes[0].percent, -50)
        fourth = plan('projection', monthsDelta=2)
        context = resolve_context(fourth, context)
        self.assertEqual(fourth.analyses[0].months, 5)
        self.assertEqual(context['protectedCategories'], ['房租', '餐饮'])
        self.assertEqual(len(context['changes']), 1)
        self.assertNotIn('monthsDelta', context)

    def test_repeated_source_adjustment_replaces_instead_of_compounding(self):
        context = resolve_context(plan('projection', months=3, changes=[{'metric': 'income', 'source': '家教', 'percent': -50}]), {})
        result = resolve_context(plan('projection', changes=[{'metric': 'income', 'source': '家教', 'percent': -20}]), context)
        self.assertEqual(len(result['changes']), 1)
        self.assertEqual(result['changes'][0]['percent'], -20)

    def test_new_question_and_explicit_reset_clear_old_scenarios(self):
        previous = {'months': 3, 'startingBalance': 10000, 'changes': [{'metric': 'income', 'percent': -50, 'source': '家教'}]}
        self.assertNotIn('startingBalance', resolve_context(plan('purchase', follow=False, purchaseAmount=6000), previous))
        reset = plan('projection', months=2)
        reset.resetAnalysisContext = True
        self.assertEqual(resolve_context(reset, previous)['changes'], [])

    def test_extension_without_previous_horizon_is_not_guessed(self):
        with self.assertRaises(ValueError):
            resolve_context(plan('projection', monthsDelta=2), {})

    def test_changed_query_scope_does_not_inherit_assumptions(self):
        previous = {'months': 3, 'changes': [{'metric': 'income', 'percent': -50, 'source': '家教'}],
                    'scope': [['search_personal_orders', '不限', '']]}
        current = plan('projection', months=2)
        from app.schemas.plan import ToolCall, SearchPersonalOrdersArgs
        current.toolCalls = [ToolCall(toolName='search_personal_orders', args=SearchPersonalOrdersArgs(cost_type='餐饮'))]
        self.assertEqual(resolve_context(current, previous)['changes'], [])
