from functools import lru_cache

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.schemas.plan import AgentPlan


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
    definitions = schema.get('$defs', {})
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
    return _compatible(settings)


@lru_cache
def get_planner_model() -> Runnable:
    settings = get_settings()
    if settings.llm_provider == "ollama":
        return _ollama(settings, planner=True)
    return _compatible(settings, planner=True).bind(response_format={"type": "json_object"})


@lru_cache
def get_structured_model(schema: type, *, reasoning: bool | None = None, num_predict: int | None = None) -> Runnable:
    settings = get_settings()
    if settings.llm_provider == "ollama":
        model = _ollama(settings, planner=True)
        options = {}
        if reasoning is not None:
            options['reasoning'] = reasoning
        if num_predict is not None:
            options['num_predict'] = num_predict
        return model.model_copy(update=options).bind(format=_execution_schema(schema.model_json_schema()))
    model = _compatible(settings, planner=True)
    if num_predict is not None:
        model = model.model_copy(update={'max_tokens': num_predict})
    return model.bind(response_format={"type": "json_object"})


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
    )
