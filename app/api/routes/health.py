from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.services.ollama_health import check_ollama


router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    ollama_status = await check_ollama()

    return {
        "service": "ok",
        "model": settings.ollama_model,
        "ollama_base_url": settings.ollama_base_url,
        "ollama": ollama_status.status,
        "error": ollama_status.error,
    }
