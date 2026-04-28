import httpx
from pydantic import BaseModel

from app.core.config import get_settings


class OllamaStatus(BaseModel):
    status: str
    error: str | None = None


async def check_ollama() -> OllamaStatus:
    settings = get_settings()

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return OllamaStatus(status="unavailable", error=str(exc))

    return OllamaStatus(status="ok")
