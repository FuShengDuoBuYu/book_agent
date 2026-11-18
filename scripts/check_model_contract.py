"""Probe structured decoding without a ledger; run before model/runtime changes."""

import asyncio
import json
from time import perf_counter
from typing import Literal

from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings
from app.core.llm import get_structured_model


class ContractProbe(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sentinel: Literal['BOOK_AGENT_PROBE']


async def probe_contract(model=None):
    started = perf_counter()
    try:
        model = model if model is not None else get_structured_model(ContractProbe)
        # Deliberately do not ask for JSON in the prompt. A runtime that silently
        # drops the schema must fail this probe rather than pass by cooperation.
        response = await asyncio.wait_for(model.ainvoke([
            ('human', '只回答一句普通中文：准备好了。')]),
            timeout=get_settings().planner_timeout_seconds)
        ContractProbe.model_validate_json(str(response.content))
        result = {'passed': True}
    except Exception as exc:
        result = {'passed': False, 'error': type(exc).__name__}
    return {**result, 'durationMs': round((perf_counter() - started) * 1000)}


async def main():
    settings = get_settings()
    result = {**await probe_contract(), 'provider': settings.llm_provider, 'model': settings.llm_model}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    with tracing_context(enabled=False):
        raise SystemExit(asyncio.run(main()))
