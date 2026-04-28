import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.clients.bookkeeping_api import BookkeepingApiClient
from app.schemas.orders import SearchOrdersRequest, SearchOrdersResponse


async def search_orders(
    phone_num: str,
    mode: str,
    family_id: str = "",
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
    cost_type: str = "不限",
    remark: str = "",
) -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    book_mode = normalize_mode(mode)
    resolved_family_id = str(family_id or "").strip()

    if book_mode == "家庭版" and not resolved_family_id:
        resolved_family_id = await resolve_family_id(phone_num)
        if not resolved_family_id:
            return to_json(
                {
                    "ok": False,
                    "error": "missing_family_id",
                    "message": "家庭版查询需要 familyId，但当前用户资料中没有找到 familyId。",
                    "mode": book_mode,
                    "familyIdResolved": False,
                    "orderCount": 0,
                    "source": "GET /user/getUser/{phone_num}",
                }
            )

    normalized_cost_type = normalize_cost_type(cost_type)
    should_filter_locally = year is None and (month is not None or day is not None)
    request = SearchOrdersRequest(
        mode=book_mode,
        familyId=resolved_family_id,
        phoneNum=phone_num,
        userId=phone_num,
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
        data = await BookkeepingApiClient().search_orders(request)
    except httpx.HTTPStatusError as exc:
        return to_json(
            {
                "ok": False,
                "error": "bookkeeping_api_http_error",
                "status_code": exc.response.status_code,
                "detail": exc.response.text,
                "mode": book_mode,
                "familyIdSet": bool(resolved_family_id),
                "query": request.model_dump(by_alias=True),
            }
        )
    except httpx.HTTPError as exc:
        return to_json(
            {
                "ok": False,
                "error": "bookkeeping_api_request_error",
                "detail": str(exc),
                "mode": book_mode,
                "familyIdSet": bool(resolved_family_id),
                "query": request.model_dump(by_alias=True),
            }
        )

    try:
        parsed = SearchOrdersResponse.model_validate(data)
    except ValueError:
        return to_json(
            {
                "ok": False,
                "error": "unexpected_bookkeeping_api_response",
                "mode": book_mode,
                "familyIdSet": bool(resolved_family_id),
                "query": request.model_dump(by_alias=True),
                "raw": data,
            }
        )

    orders = [order.model_dump() for order in parsed.data.ordersInfo]
    if should_filter_locally:
        orders = filter_orders_locally(orders, month=month, day=day)

    return to_json(
        {
            "ok": parsed.success,
            "message": parsed.message,
            "mode": book_mode,
            "familyIdSet": bool(resolved_family_id),
            "familyIdResolved": book_mode == "家庭版" and bool(resolved_family_id),
            "query": request.model_dump(by_alias=True),
            "localFilter": {
                "enabled": should_filter_locally,
                "month": month,
                "day": day,
            },
            "normalizedCostType": normalized_cost_type,
            "userInfo": parsed.data.userInfo,
            "ordersInfo": orders,
            "orderCount": len(orders),
            "source": "POST /searchOrders",
        }
    )


def to_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def mask_phone(value: str) -> str:
    phone = str(value or "")
    if len(phone) <= 4:
        return phone
    return f"***{phone[-4:]}"


def mask_trace_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(inputs)
    if "phone_num" in cleaned:
        cleaned["phone_num"] = mask_phone(cleaned["phone_num"])
    if "family_id" in cleaned and cleaned["family_id"]:
        cleaned["family_id"] = "***"
    return cleaned


def normalize_mode(value: str | None) -> str:
    mode = str(value or "").strip()
    if mode in {"家庭版", "家庭", "family", "family_mode"}:
        return "家庭版"
    return "个人版"


async def resolve_family_id(phone_num: str) -> str:
    try:
        data = await BookkeepingApiClient().get_user(phone_num)
    except (httpx.HTTPError, ValueError):
        return ""

    user = data.get("data") if isinstance(data, dict) else None
    if not isinstance(user, dict):
        return ""
    return str(user.get("familyId") or "").strip()


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
