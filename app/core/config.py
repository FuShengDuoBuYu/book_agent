import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    app_name: str = "book_agent"
    app_version: str = "0.1.0"
    bookkeeping_api_base_url: str = "https://autobookkeeping-fastapi.onrender.com"
    agent_mongodb_uri: str = ""
    agent_mongodb_db: str = "book_agent"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:14b"
    ollama_temperature: float = 0.2
    ollama_num_ctx: int = 8192
    planner_timeout_seconds: float = 20.0
    planner_num_predict: int = 512
    request_timeout_seconds: float = 120.0
    frontend_dist: Path = BASE_DIR / "frontend" / "dist"


@lru_cache
def get_settings() -> Settings:
    defaults = Settings()
    return Settings(
        bookkeeping_api_base_url=os.getenv(
            "BOOKKEEPING_API_BASE_URL", defaults.bookkeeping_api_base_url
        ),
        agent_mongodb_uri=os.getenv("AGENT_MONGODB_URI", defaults.agent_mongodb_uri),
        agent_mongodb_db=os.getenv("AGENT_MONGODB_DB", defaults.agent_mongodb_db),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", defaults.ollama_base_url),
        ollama_model=os.getenv("OLLAMA_MODEL", defaults.ollama_model),
        ollama_temperature=float(
            os.getenv("OLLAMA_TEMPERATURE", str(defaults.ollama_temperature))
        ),
        ollama_num_ctx=int(os.getenv("OLLAMA_NUM_CTX", str(defaults.ollama_num_ctx))),
        planner_timeout_seconds=float(
            os.getenv(
                "PLANNER_TIMEOUT_SECONDS", str(defaults.planner_timeout_seconds)
            )
        ),
        planner_num_predict=int(
            os.getenv("PLANNER_NUM_PREDICT", str(defaults.planner_num_predict))
        ),
    )
