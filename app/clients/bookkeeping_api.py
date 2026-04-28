from typing import Any

import httpx

from app.core.config import get_settings
from app.schemas.orders import SearchOrdersRequest


class BookkeepingApiClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.bookkeeping_api_base_url.rstrip("/")
        self.timeout = settings.request_timeout_seconds

    async def search_orders(self, request: SearchOrdersRequest) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/searchOrders",
                json=request.model_dump(by_alias=True),
            )
            response.raise_for_status()

        return response.json()
