import httpx
from fastapi import APIRouter, Response

from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"service": "ok"}


@router.get("/ready")
async def ready(response: Response) -> dict[str, str]:
    settings = get_settings()
    if not settings.agent_mongodb_uri:
        response.status_code = 503
        return {"status": "memory_not_configured"}

    native = settings.llm_provider == "ollama"
    path, list_key = ("/api/tags", "models") if native else ("/models", "data")
    name_key = "name" if native else "id"
    headers = {}
    if settings.llm_api_key and not native:
        headers["Authorization"] = f"Bearer {settings.llm_api_key.get_secret_value()}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            result = await client.get(f"{settings.llm_base_url}{path}", headers=headers)
            result.raise_for_status()
        payload = result.json()
        models = payload[list_key]
        found = any(item.get(name_key) == settings.llm_model for item in models)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        response.status_code = 503
        return {"status": "model_gateway_unavailable"}

    if not found:
        response.status_code = 503
        return {"status": "model_not_listed"}
    return {"status": "ok", "provider": settings.llm_provider, "model": settings.llm_model}
