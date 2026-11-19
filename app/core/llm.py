from functools import lru_cache

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
import httpx

from app.core.config import Settings, get_settings
from app.schemas.plan import AgentPlan
from app.core.openai_adapter import LedgerOpenAI, response_format
from app.core.model_usage import UsageRecorder


def planner_schema():
    from app.tools.registry import PLANNER_TOOL_BY_NAME
    schema = AgentPlan.model_json_schema()
    schema['required'] = ['understanding', 'output', 'intent', 'analysisType', 'needsTools',
        'toolCalls', 'analyses', 'analysisWindow', 'finalInstruction', 'followUpOfPreviousQuestion']
    schema['$defs']['ToolCall']['properties']['toolName']['enum'] = list(PLANNER_TOOL_BY_NAME)
    schema['$defs']['IntentUnderstanding']['required'] = list(schema['$defs']['IntentUnderstanding']['properties'])
    return _execution_schema(schema)


def _execution_schema(schema):
    """Native generation must fill execution fields, rather than rely on API defaults."""
    if schema.get('title') == 'IntentUnderstanding':
        schema['required'] = list(schema['properties'])
    definitions = schema.get('$defs', {})
    from app.tools.financial_analysis import ANALYSIS_TOOLS
    intent_nodes = [node for node in (schema if schema.get('title') == 'IntentUnderstanding' else None,
                                      definitions.get('IntentUnderstanding')) if node is not None]
    for node in intent_nodes:
        node['properties']['evidenceNeeds']['items']['enum'] = list(dict.fromkeys(
            ['totals', 'members', 'remarks', 'quality', *ANALYSIS_TOOLS]))
    for name, fields in {
        'AnalysisRequest': ['aggregate', 'changes', 'months'],
        'AggregateSpec': ['groupBy', 'periods', 'filters', 'limit'],
        'AggregateFilters': None,
    }.items():
        if name in definitions:
            definition = definitions[name]
            definition['required'] = list(dict.fromkeys([
                *definition.get('required', []), *(fields or definition['properties'])]))
    return schema


@lru_cache
def get_chat_model() -> BaseChatModel:
    settings = get_settings()
    if settings.llm_provider == "ollama":
        return _ollama(settings)
    if settings.llm_provider == 'openai':
        return _openai(settings)
    return _compatible(settings)


@lru_cache
def get_planner_model() -> Runnable:
    settings = get_settings()
    if settings.llm_provider == "ollama":
        return _ollama(settings, planner=True)
    if settings.llm_provider == 'openai':
        return _openai(settings, planner=True).bind(response_format=response_format('AgentPlan', planner_schema()))
    return _compatible(settings, planner=True).bind(response_format={"type": "json_object"})


@lru_cache
def get_structured_model(schema: type, *, reasoning: bool | None = None, num_predict: int | None = None,
                         temperature: float | None = None) -> Runnable:
    settings = get_settings()
    if settings.llm_provider == 'openai':
        effort = settings.llm_reasoning_effort
        if reasoning is False and settings.llm_model != 'gpt-6.2-sol':
            effort = 'none'
        model = _openai(settings, planner=True).model_copy(update={
            'max_tokens': num_predict or settings.planner_num_predict,
            'reasoning_effort': effort, 'tags': ['schema:' + schema.__name__]})
        return model.bind(response_format=response_format(schema.__name__, _execution_schema(schema.model_json_schema())))
    if settings.llm_provider == "ollama":
        model = _ollama(settings, planner=True)
        options = {}
        if reasoning is not None:
            options['reasoning'] = reasoning
        if num_predict is not None:
            options['num_predict'] = num_predict
        if temperature is not None:
            options['temperature'] = temperature
        options['tags'] = ['schema:' + schema.__name__]
        return model.model_copy(update=options).bind(format=_execution_schema(schema.model_json_schema()))
    model = _compatible(settings, planner=True)
    if num_predict is not None:
        model = model.model_copy(update={'max_tokens': num_predict})
    if temperature is not None:
        model = model.model_copy(update={'temperature': temperature})
    model = model.model_copy(update={'tags': ['schema:' + schema.__name__]})
    return model.bind(response_format={"type": "json_object"})


def _openai_http_client():
    return httpx.AsyncClient(timeout=get_settings().request_timeout_seconds, follow_redirects=False)


def _openai(settings: Settings, planner=False):
    return LedgerOpenAI(
        model=settings.llm_model, base_url=settings.llm_base_url,
        api_key=settings.llm_api_key, temperature=None,
        max_tokens=settings.planner_num_predict if planner else 2048,
        reasoning_effort=settings.llm_reasoning_effort,
        timeout=settings.request_timeout_seconds, max_retries=0,
        use_responses_api=True, output_version='v0', store=False,
        service_tier='default', http_async_client=_openai_http_client(),
        callbacks=[UsageRecorder('openai', settings.llm_model)],
        tags=['schema:AgentPlan' if planner else 'stage:chat'],
    )


def _ollama(settings: Settings, planner: bool = False) -> ChatOllama:
    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        reasoning=settings.planner_reasoning if planner else settings.llm_reasoning,
        temperature=0 if planner else settings.llm_temperature,
        num_ctx=settings.ollama_num_ctx,
        keep_alive=settings.ollama_keep_alive,
        num_gpu=settings.ollama_num_gpu,
        format=planner_schema() if planner else None,
        num_predict=settings.planner_num_predict if planner else None,
        client_kwargs={"timeout": settings.request_timeout_seconds},
        callbacks=[UsageRecorder('ollama', settings.llm_model)],
        tags=['schema:AgentPlan' if planner else 'stage:chat'],
    )


def _compatible(settings: Settings, planner: bool = False) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or SecretStr("ollama"),
        temperature=0 if planner else settings.llm_temperature,
        max_tokens=settings.planner_num_predict if planner else None,
        reasoning_effort=settings.llm_reasoning_effort,
        timeout=settings.request_timeout_seconds,
        max_retries=0,
        use_responses_api=False,
        callbacks=[UsageRecorder('openai_compatible', settings.llm_model)],
        tags=['schema:AgentPlan' if planner else 'stage:chat'],
    )
