from functools import lru_cache

from langchain_ollama import ChatOllama

from app.core.config import get_settings


@lru_cache
def get_chat_model() -> ChatOllama:
    settings = get_settings()
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        temperature=settings.ollama_temperature,
    )


@lru_cache
def get_planner_model() -> ChatOllama:
    settings = get_settings()
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        reasoning=False,
        format="json",
        temperature=0,
        num_predict=settings.planner_num_predict,
    )
