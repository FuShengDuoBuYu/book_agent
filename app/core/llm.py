from functools import lru_cache

from langchain_ollama import ChatOllama

from app.core.config import get_settings


@lru_cache
def get_chat_model() -> ChatOllama:
    # 主对话模型负责“最终回答”，允许正常生成自然语言。
    # 用 lru_cache 复用模型实例，避免每次请求重复创建。
    settings = get_settings()
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        reasoning=False,
        temperature=settings.ollama_temperature,
        num_ctx=settings.ollama_num_ctx,
    )


@lru_cache
def get_planner_model() -> ChatOllama:
    # Planner 模型只负责产出结构化计划，所以把温度压低并强制 JSON 输出。
    settings = get_settings()
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        reasoning=False,
        format="json",
        temperature=0,
        num_ctx=settings.ollama_num_ctx,
        num_predict=settings.planner_num_predict,
    )
