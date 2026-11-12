from typing import Any

import httpx

from app.core.config import get_settings
from app.schemas.orders import SearchOrdersRequest


class BookkeepingApiClient:
    def __init__(self, session_token: str | None = None) -> None:
        settings = get_settings()
        self.base_url = settings.bookkeeping_api_base_url
        self.timeout = settings.request_timeout_seconds
        self._session_token = session_token

    async def exchange_ticket(self, ticket: str) -> dict[str, Any]:
        return await self._request("POST", "/ai-bill/session", {"ticket": ticket})

    async def get_session(self) -> dict[str, Any]:
        return await self._request("GET", "/ai-bill/session")

    async def search_orders(self, request: SearchOrdersRequest) -> dict[str, Any]:
        return await self._request(
            "POST", "/ai-bill/orders", request.model_dump(by_alias=True)
        )

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {}
        if self._session_token:
            headers["Authorization"] = f"Bearer {self._session_token}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method, f"{self.base_url}{path}", headers=headers, json=payload
            )
            response.raise_for_status()
        return response.json()
