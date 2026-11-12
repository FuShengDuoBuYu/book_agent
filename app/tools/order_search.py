from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.clients.bookkeeping_api import BookkeepingApiClient
from app.schemas.orders import SearchOrdersRequest, SearchOrdersResponse


async def search_orders(
    client: BookkeepingApiClient,
    mode: str,
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
    cost_type: str = "不限",
    remark: str = "",
    *,
    family_scope: str = "",
) -> dict[str, Any]:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    normalized_cost_type = normalize_cost_type(cost_type)
    should_filter_locally = year is None and (month is not None or day is not None)
    request = SearchOrdersRequest(
        mode=mode,
        year=year or now.year,
        month=month or now.month,
        day=day or now.day,
        searchOrderRemark=remark,
        searchCostType=normalized_cost_type,
        ifIgnoreYear=year is None,
        ifIgnoreMonth=month is None,
        ifIgnoreDay=day is None,
    )
    try:
        data = await client.search_orders(request, family_scope=family_scope)
        parsed = SearchOrdersResponse.model_validate(data)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403, 409}:
            return {"ok": False, "error": "bookkeeping_access_denied"}
        return {"ok": False, "error": "bookkeeping_unavailable"}
    except (httpx.HTTPError, ValueError):
        return {"ok": False, "error": "bookkeeping_unavailable"}

    orders = [order.model_dump(exclude={"userId"}) for order in parsed.data.ordersInfo]
    if should_filter_locally:
        orders = filter_orders_locally(orders, month=month, day=day)

    return {
        "ok": parsed.success,
        "mode": mode,
        "query": request.model_dump(by_alias=True),
        "localFilter": {
            "enabled": should_filter_locally,
            "month": month,
            "day": day,
        },
        "normalizedCostType": normalized_cost_type,
        "userFound": bool(parsed.data.userInfo),
        "ordersInfo": orders,
        "orderCount": len(orders),
    }


def normalize_cost_type(value: str | None) -> str:
    cost_type = str(value or "").strip()
    if not cost_type:
        return "不限"

    broad_words = {
        "不限",
        "全部",
        "所有",
        "支出",
        "花费",
        "消费",
        "开销",
        "费用",
        "明细",
        "支出明细",
        "消费明细",
        "花费明细",
        "开销明细",
        "各分类",
        "分类",
    }
    return "不限" if cost_type in broad_words else cost_type


def filter_orders_locally(
    orders: list[dict[str, Any]],
    month: int | None = None,
    day: int | None = None,
) -> list[dict[str, Any]]:
    filtered_orders = orders
    if month is not None:
        filtered_orders = [
            order for order in filtered_orders if int(order.get("month") or 0) == month
        ]
    if day is not None:
        filtered_orders = [
            order for order in filtered_orders if int(order.get("day") or 0) == day
        ]
    return filtered_orders
