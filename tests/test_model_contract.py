import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock


class ModelContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_natural_prose_does_not_pass_a_requested_structured_contract(self):
        from scripts.check_model_contract import probe_contract
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content='准备好了。')))
        result = await probe_contract(model)
        self.assertFalse(result['passed'])

    async def test_valid_json_still_has_to_obey_the_requested_shape(self):
        from scripts.check_model_contract import probe_contract
        for content, expected in (('{"sentinel":"BOOK_AGENT_PROBE"}', True),
                                  ('{"sentinel":"wrong"}', False),
                                  ('{"sentinel":"BOOK_AGENT_PROBE","extra":1}', False)):
            model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=content)))
            self.assertEqual((await probe_contract(model))['passed'], expected)
