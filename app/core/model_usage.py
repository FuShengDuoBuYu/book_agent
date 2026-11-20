"""Per-turn model accounting, independent of tracing and application settings.

Only counters and a small, explicit metadata allowlist leave this module. Output
tokens already include reasoning tokens; cached reads and writes are part of
input tokens. Prices are Standard USD per million tokens as of 2026-11-19.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
import re
from time import perf_counter
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler


OPENAI_PRICE_DATE = '2026-11-19'
OPENAI_MODELS = {
    'gpt-6-luna': {'input': Decimal('.1'), 'output': Decimal('.5'),
        'cache_read': Decimal('.01'), 'cache_write_30m': Decimal('.125'), 'cache_write_12h': Decimal('.25')},
    'gpt-6.1-terra': {'input': Decimal('1'), 'output': Decimal('5'),
        'cache_read': Decimal('.05'), 'cache_write_30m': Decimal('1.25'), 'cache_write_12h': Decimal('2.5')},
    'gpt-6.2-sol': {'input': Decimal('2'), 'output': Decimal('10'),
        'cache_read': Decimal('.1'), 'cache_write_30m': Decimal('2.5'), 'cache_write_12h': Decimal('5')},
}
_MILLION = Decimal(1_000_000)
_CURRENT: ContextVar['TurnUsage | None'] = ContextVar('book_agent_model_usage', default=None)
_TOKEN_FIELDS = ('inputTokens', 'outputTokens', 'reasoningTokens', 'cachedInputTokens', 'cacheWriteTokens')


def current_usage() -> 'TurnUsage | None':
    return _CURRENT.get()


@contextmanager
def capture_usage(usage: 'TurnUsage | None' = None) -> Iterator['TurnUsage']:
    """Bind a fresh turn, or rebind a turn while advancing a stream.

    Enter and leave in the same context. In particular, do not yield from an
    async generator inside this context manager: its caller can close it from
    a different task. Child asyncio tasks inherit the same TurnUsage object.
    """
    bound = usage if usage is not None else TurnUsage()
    token = _CURRENT.set(bound)
    try:
        yield bound
    finally:
        _CURRENT.reset(token)


@dataclass
class _Tokens:
    input: int | None = None
    output: int | None = None
    reasoning: int | None = None
    cached: int | None = None
    write: int | None = None
    write_30m: int | None = None
    write_12h: int | None = None


@dataclass
class _Call:
    provider: str
    model: str
    stage: str
    started: float
    status: str = 'running'
    elapsed_ms: float | None = None
    tokens: _Tokens | None = None
    error_type: str | None = None

    def cost(self) -> tuple[Decimal | None, str]:
        if self.provider == 'ollama':
            # Local execution has no provider API charge, including failed calls.
            # This excludes electricity and hardware costs.
            return Decimal(0), 'local_no_api_charge'
        if self.status != 'completed':
            return None, 'unknown_after_error' if self.status == 'error' else 'incomplete_call'
        if self.provider != 'openai' or self.model not in OPENAI_MODELS:
            return None, 'model_price_unavailable'
        t = self.tokens
        if t is None or t.input is None or t.output is None:
            return None, 'usage_unavailable'
        cached, write = t.cached or 0, t.write or 0
        write_30m, write_12h = t.write_30m or 0, t.write_12h or 0
        unspecified = write - write_30m - write_12h
        regular = t.input - cached - write
        if regular < 0 or unspecified < 0 or (t.reasoning or 0) > t.output:
            return None, 'invalid_usage'
        prices = OPENAI_MODELS[self.model]
        cost = (Decimal(regular) * prices['input']
                + Decimal(t.output) * prices['output']
                + Decimal(cached) * prices['cache_read']
                + Decimal(write_30m) * prices['cache_write_30m']
                + Decimal(write_12h + unspecified) * prices['cache_write_12h']) / _MILLION
        basis = ('openai_standard_conservative_cache_write_12h_estimate' if unspecified
                 else 'openai_standard_estimate')
        return cost, basis

    def snapshot(self) -> dict[str, Any]:
        cost, basis = self.cost()
        t = self.tokens
        row = {
            'provider': self.provider, 'model': self.model, 'stage': self.stage,
            'status': self.status,
            'latencyMs': self.elapsed_ms if self.elapsed_ms is not None
                else round(max(0, perf_counter() - self.started) * 1000, 3),
            'inputTokens': t.input if t else None, 'outputTokens': t.output if t else None,
            'reasoningTokens': (t.reasoning or 0) if t else None,
            'cachedInputTokens': (t.cached or 0) if t else None,
            'cacheWriteTokens': (t.write or 0) if t else None,
            'cacheWrite30mTokens': (t.write_30m or 0) if t else None,
            'cacheWrite12hTokens': (t.write_12h or 0) if t else None,
            'estimatedCostUsd': float(cost) if cost is not None else None,
            'costBasis': basis,
        }
        if self.provider == 'openai' and self.model in OPENAI_MODELS:
            row['priceDate'] = OPENAI_PRICE_DATE
        if self.error_type:
            row['errorType'] = self.error_type
        return row


class TurnUsage:
    """One mutable collection shared by a turn's child tasks, never by turns."""

    def __init__(self) -> None:
        self._calls: dict[Any, _Call] = {}

    def _start(self, run_id: Any, provider: str, model: str, stage: str) -> None:
        if run_id not in self._calls:
            self._calls[run_id] = _Call(provider, model, stage, perf_counter())

    def _finish(self, run_id: Any, response: Any = None, error: BaseException | None = None) -> None:
        call = self._calls.get(run_id)
        if call is None or call.status != 'running':
            return
        call.elapsed_ms = round(max(0, perf_counter() - call.started) * 1000, 3)
        call.status = 'error' if error is not None else 'completed'
        if error is not None:
            # Exception text, stack traces, request objects and provider payloads
            # can contain credentials or user data; retain only a type name.
            call.error_type = re.sub(r'[^A-Za-z0-9_]', '', type(error).__name__)[:128] or 'Error'
        else:
            call.tokens = _result_tokens(response)

    def snapshot(self) -> dict[str, Any]:
        calls = [call.snapshot() for call in self._calls.values()]
        costs = [call.cost()[0] for call in self._calls.values()]
        bases = {row['costBasis'] for row in calls}
        basis = (next(iter(bases)) if len(bases) == 1 else
                 'no_model_calls' if not calls else 'mixed_or_unknown_estimate')
        totals = {field: (sum(row[field] for row in calls) if all(row[field] is not None for row in calls)
                          else None) for field in _TOKEN_FIELDS}
        return {'callCount': len(calls), **totals,
                'estimatedCostUsd': (float(sum(costs, Decimal(0))) if all(cost is not None for cost in costs)
                                     else None),
                'costBasis': basis, 'calls': calls}


class UsageRecorder(AsyncCallbackHandler):
    """A reusable model callback; all request state lives in the current turn."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    async def on_chat_model_start(self, serialized, messages, *, run_id, tags=None, **kwargs) -> None:
        self._start(run_id, tags, kwargs.get('invocation_params'))

    async def on_llm_start(self, serialized, prompts, *, run_id, tags=None, **kwargs) -> None:
        self._start(run_id, tags, kwargs.get('invocation_params'))

    def _start(self, run_id, tags, invocation_params) -> None:
        usage = current_usage()
        if usage is not None:
            usage._start(run_id, self.provider, self.model, _stage(tags, invocation_params))

    async def on_llm_end(self, response, *, run_id, **kwargs) -> None:
        usage = current_usage()
        if usage is not None:
            usage._finish(run_id, response=response)

    async def on_llm_error(self, error: BaseException, *, run_id, **kwargs) -> None:
        usage = current_usage()
        if usage is not None:
            usage._finish(run_id, error=error)


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _count(*values: Any) -> int | None:
    return next((value for value in values if isinstance(value, int) and not isinstance(value, bool) and value >= 0), None)


def _stage(tags: Any, invocation_params: Any) -> str:
    candidates = [tag.removeprefix('schema:') for tag in (tags or [])
                  if isinstance(tag, str) and tag.startswith('schema:')]
    params = _mapping(invocation_params)
    response_format = _mapping(params.get('response_format'))
    text_format = _mapping(_mapping(params.get('text')).get('format'))
    candidates.extend([_mapping(response_format.get('json_schema')).get('name'),
                       response_format.get('name'), text_format.get('name'),
                       _mapping(params.get('format')).get('title')])
    return next((value for value in candidates if isinstance(value, str)
                 and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', value)), 'chat')


def _parse_tokens(value: Any) -> _Tokens | None:
    data = _mapping(value)
    input_tokens = _count(data.get('input_tokens'), data.get('prompt_tokens'), data.get('prompt_eval_count'))
    output_tokens = _count(data.get('output_tokens'), data.get('completion_tokens'), data.get('eval_count'))
    if input_tokens is None and output_tokens is None:
        return None
    inputs = _mapping(data.get('input_token_details') or data.get('input_tokens_details') or data.get('prompt_tokens_details'))
    outputs = _mapping(data.get('output_token_details') or data.get('output_tokens_details') or data.get('completion_tokens_details'))
    cached = _count(inputs.get('cache_read'), inputs.get('cached_tokens'), data.get('cache_read_input_tokens'))
    write = _count(inputs.get('cache_creation'), inputs.get('cache_creation_tokens'), inputs.get('cache_write_tokens'),
                   data.get('cache_creation_input_tokens'))
    detail_sources = [inputs, data, _mapping(inputs.get('cache_creation')), _mapping(data.get('cache_creation')),
                      _mapping(inputs.get('cache_write_tokens_details'))]
    write_30m = _count(*(source.get(key) for source in detail_sources for key in
        ('ttl_30m_tokens', 'ephemeral_30m_input_tokens', 'cache_creation_30m_input_tokens', 'cache_write_30m_tokens', '30m')))
    write_12h = _count(*(source.get(key) for source in detail_sources for key in
        ('ttl_12h_tokens', 'ephemeral_12h_input_tokens', 'cache_creation_12h_input_tokens', 'cache_write_12h_tokens', '12h')))
    return _Tokens(input_tokens, output_tokens, _count(outputs.get('reasoning'), outputs.get('reasoning_tokens')),
                   cached, write, write_30m, write_12h)


def _merge_tokens(preferred: _Tokens | None, fallback: _Tokens | None) -> _Tokens | None:
    if preferred is None:
        return fallback
    if fallback is None:
        return preferred
    return _Tokens(**{field: getattr(preferred, field) if getattr(preferred, field) is not None
                     else getattr(fallback, field) for field in _Tokens.__dataclass_fields__})


def _result_tokens(response: Any) -> _Tokens | None:
    # LLMResult contains a list per prompt, with another list of candidate
    # generations. Chat models normally return [[ChatGeneration(message=...)]].
    # Usage is response-wide, so do not add alternative generations together.
    tokens = None
    for group in getattr(response, 'generations', []) or []:
        for generation in group if isinstance(group, (list, tuple)) else [group]:
            message = getattr(generation, 'message', None)
            metadata = _mapping(getattr(message, 'response_metadata', None))
            raw = _merge_tokens(_parse_tokens(metadata.get('usage')), _parse_tokens(metadata.get('token_usage')))
            raw = _merge_tokens(raw, _parse_tokens(metadata))
            tokens = _merge_tokens(_parse_tokens(getattr(message, 'usage_metadata', None)), raw)
            if tokens is not None:
                break
        if tokens is not None:
            break
    llm_output = _mapping(getattr(response, 'llm_output', None))
    fallback = _merge_tokens(_parse_tokens(llm_output.get('usage')), _parse_tokens(llm_output.get('token_usage')))
    tokens = _merge_tokens(tokens, _merge_tokens(fallback, _parse_tokens(llm_output)))
    if tokens is not None and tokens.write is None:
        tokens.write = (tokens.write_30m or 0) + (tokens.write_12h or 0)
    return tokens
