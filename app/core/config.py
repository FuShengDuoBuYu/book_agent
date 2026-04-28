import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    app_name: str = "book_agent"
    app_version: str = "0.1.0"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:14b"
    ollama_temperature: float = 0.2
    request_timeout_seconds: float = 120.0
    frontend_dist: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@lru_cache
def get_settings() -> Settings:
    defaults = Settings()
    return Settings(
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", defaults.ollama_base_url),
        ollama_model=os.getenv("OLLAMA_MODEL", defaults.ollama_model),
        ollama_temperature=float(
            os.getenv("OLLAMA_TEMPERATURE", str(defaults.ollama_temperature))
        ),
    )
