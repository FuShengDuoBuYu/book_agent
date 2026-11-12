from functools import lru_cache

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.schemas.plan import AgentPlan


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
def get_structured_model(schema: type) -> Runnable:
    settings = get_settings()
    if settings.llm_provider == "ollama":
        return _ollama(settings, planner=True).bind(format=schema.model_json_schema())
    return _compatible(settings, planner=True).bind(response_format={"type": "json_object"})


def _ollama(settings: Settings, planner: bool = False) -> ChatOllama:
    return ChatOllama(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        reasoning=False if planner else settings.llm_reasoning,
        temperature=0 if planner else settings.llm_temperature,
        num_ctx=settings.ollama_num_ctx,
        format=AgentPlan.model_json_schema() if planner else None,
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
