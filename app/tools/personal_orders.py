import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.clients.bookkeeping_api import BookkeepingApiClient
from app.schemas.orders import SearchOrdersRequest, SearchOrdersResponse


class SearchPersonalOrdersInput(BaseModel):
    phone_num: str = Field(description="用户手机号，只允许查询当前用户自己的手机号")
    year: int | None = Field(default=None, description="查询年份，例如 2026")
    month: int | None = Field(default=None, description="查询月份，1-12")
    day: int | None = Field(default=None, description="查询日期，1-31；不传则查询整月")
    cost_type: str = Field(default="不限", description="账单分类，不限表示不过滤")
    remark: str = Field(default="", description="备注关键词，不传则不过滤")


async def search_personal_orders(
    phone_num: str,
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
    cost_type: str = "不限",
    remark: str = "",
) -> str:
    """查询某个用户的个人账单。

    该工具只查询个人版账单，不查询家庭账本。返回远程记账 API 的 JSON 结果。
    """

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    should_filter_locally = year is None and (month is not None or day is not None)
    request = SearchOrdersRequest(
        phoneNum=phone_num,
        userId=phone_num,
        year=year or now.year,
        month=month or now.month,
        day=day or now.day,
        searchOrderRemark=remark,
        searchCostType=cost_type or "不限",
        ifIgnoreYear=year is None,
        ifIgnoreMonth=month is None,
        ifIgnoreDay=day is None,
    )

    try:
        data = await BookkeepingApiClient().search_orders(request)
    except httpx.HTTPStatusError as exc:
        return _to_json(
            {
                "ok": False,
                "error": "bookkeeping_api_http_error",
                "status_code": exc.response.status_code,
                "detail": exc.response.text,
                "query": request.model_dump(by_alias=True),
            }
        )
    except httpx.HTTPError as exc:
        return _to_json(
            {
                "ok": False,
                "error": "bookkeeping_api_request_error",
                "detail": str(exc),
                "query": request.model_dump(by_alias=True),
            }
        )

    try:
        parsed = SearchOrdersResponse.model_validate(data)
    except ValueError:
        return _to_json(
            {
                "ok": False,
                "error": "unexpected_bookkeeping_api_response",
                "query": request.model_dump(by_alias=True),
                "raw": data,
            }
        )

    orders = [order.model_dump() for order in parsed.data.ordersInfo]
    if should_filter_locally:
        orders = _filter_orders_locally(orders, month=month, day=day)

    return _to_json(
        {
            "ok": parsed.success,
            "message": parsed.message,
            "query": request.model_dump(by_alias=True),
            "localFilter": {
                "enabled": should_filter_locally,
                "month": month,
                "day": day,
            },
            "userInfo": parsed.data.userInfo,
            "ordersInfo": orders,
            "orderCount": len(orders),
            "source": "POST /searchOrders",
        }
    )


def _to_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _filter_orders_locally(
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


search_personal_orders_tool = StructuredTool.from_function(
    coroutine=search_personal_orders,
    name="search_personal_orders",
    description=(
        "查询某个手机号对应用户的个人账单。可按年、月、日、分类、备注关键词过滤。"
        "只用于个人版账单，不用于家庭账本。"
    ),
    args_schema=SearchPersonalOrdersInput,
)
