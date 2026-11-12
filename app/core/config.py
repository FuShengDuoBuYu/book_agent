import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    app_name: str = "book_agent"
    app_version: str = "0.1.0"
    bookkeeping_api_base_url: str = "https://autobookkeeping-fastapi.onrender.com"
    agent_mongodb_uri: str = ""
    agent_mongodb_db: str = "book_agent"
    llm_provider: Literal["ollama", "openai_compatible"] = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = Field(default="qwen3:14b", min_length=1)
    llm_api_key: SecretStr | None = None
    llm_temperature: float = Field(default=0.2, ge=0, le=2)
    llm_reasoning_effort: str | None = None
    llm_reasoning: bool = False
    ollama_num_ctx: int = Field(default=8192, gt=0)
    planner_timeout_seconds: float = Field(default=20.0, gt=0)
    planner_num_predict: int = Field(default=2048, gt=0)
    request_timeout_seconds: float = Field(default=120.0, gt=0)
    frontend_dist: Path = BASE_DIR / "frontend" / "dist"

    @field_validator("llm_base_url", "bookkeeping_api_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        url = urlsplit(value.strip())
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("配置地址必须为 HTTP(S) 基础 URL，凭证由独立环境变量提供")
        return value.strip().rstrip("/")


@lru_cache
def get_settings() -> Settings:
    defaults = Settings()
    provider = os.getenv("LLM_PROVIDER", defaults.llm_provider)
    if provider == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", defaults.llm_base_url)
        model = os.getenv("OLLAMA_MODEL", defaults.llm_model)
    else:
        base_url = ""
        model = ""

    return Settings(
        bookkeeping_api_base_url=os.getenv(
            "BOOKKEEPING_API_BASE_URL", defaults.bookkeeping_api_base_url
        ),
        agent_mongodb_uri=os.getenv("AGENT_MONGODB_URI", defaults.agent_mongodb_uri),
        agent_mongodb_db=os.getenv("AGENT_MONGODB_DB", defaults.agent_mongodb_db),
        llm_provider=provider,
        llm_base_url=os.getenv("LLM_BASE_URL") or base_url,
        llm_model=os.getenv("LLM_MODEL") or model,
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_temperature=os.getenv("LLM_TEMPERATURE")
        or os.getenv("OLLAMA_TEMPERATURE", str(defaults.llm_temperature)),
        llm_reasoning_effort=os.getenv("LLM_REASONING_EFFORT") or None,
        llm_reasoning=os.getenv("LLM_REASONING", str(defaults.llm_reasoning)),
        ollama_num_ctx=os.getenv("OLLAMA_NUM_CTX", str(defaults.ollama_num_ctx)),
        planner_timeout_seconds=os.getenv(
            "PLANNER_TIMEOUT_SECONDS", str(defaults.planner_timeout_seconds)
        ),
        planner_num_predict=os.getenv("PLANNER_NUM_PREDICT", str(defaults.planner_num_predict)),
        request_timeout_seconds=os.getenv(
            "REQUEST_TIMEOUT_SECONDS", str(defaults.request_timeout_seconds)
        ),
    )
