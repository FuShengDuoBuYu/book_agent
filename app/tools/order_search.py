import hashlib
import hmac
import os
import re
import secrets
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.clients.bookkeeping_api import BookkeepingApiClient
from app.schemas.orders import SearchOrdersRequest, SearchOrdersResponse


# Provision this secret once per deployment so restored conversations and workers
# use the same pseudonyms. Development sessions can operate without persistent keys.
_MEMBER_SECRET = os.environ.get("AGENT_MEMBER_KEY_SECRET", "").encode() or secrets.token_bytes(32)
_PRIVATE_NUMBER = r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d[ -]?\d{4}[ -]?\d{4}(?!\d)|(?<!\d)\d{12,19}(?!\d)"


def _opaque_key(kind: str, value: str) -> str:
    digest = hmac.new(_MEMBER_SECRET, (kind + "\0" + value).encode(), hashlib.sha256)
    return kind + "_" + digest.hexdigest()[:24]


def _public_orders(data, family_scope: str, query: dict) -> tuple[list[dict], dict]:
    profiles = {str(user["phoneNum"]): user for user in data.userInfo if user.get("phoneNum")}
    private_values = set(profiles) | {order.userId for order in data.ordersInfo} | {family_scope}
    private_values.update(str(user.get("familyId") or "") for user in data.userInfo)
    patterns = [re.escape(value) for value in sorted(private_values - {""}, key=len, reverse=True)]
    private = re.compile("|".join([_PRIVATE_NUMBER, *patterns]))

    def redact(value: str) -> str:
        return private.sub("[已隐藏]", value)

    members = {}
    names = defaultdict(list)
    for owner in profiles.keys() | {order.userId for order in data.ordersInfo}:
        key = _opaque_key("member", owner) if owner else "member_unknown"
        profile = profiles.get(owner, {})
        candidates = [str(profile.get(field) or "").strip() for field in ("nickname", "familyIdentity")]
        name = next((label[:48] for label in candidates if label and not private.search(label)), "")
        members[owner] = (key, name or ("未知成员 " + key[-6:] if owner else "未知成员"))
        if name:
            names[name].append((key, owner))
    for name, owners in names.items():
        if len(owners) > 1:
            for index, (key, owner) in enumerate(sorted(owners), 1):
                members[owner] = (key, f"{name}（成员{index}）")

    records = []
    for order in data.ordersInfo:
        row = order.model_dump(exclude={"userId", "id"})
        for field in ("orderRemark", "costType", "bankName", "clock"):
            row[field] = redact(row[field])
        # Distinct ledger rows can have identical public fields when queries overlap.
        if order.id is not None:
            row["id"] = _opaque_key("order", str(order.id))
        row["memberKey"], row["memberName"] = members[order.userId]
        records.append(row)
    return records, {key: redact(value) if isinstance(value, str) else value for key, value in query.items()}


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

    orders, public_query = _public_orders(parsed.data, family_scope, request.model_dump(by_alias=True))
    if should_filter_locally:
        orders = filter_orders_locally(orders, month=month, day=day)

    return {
        "ok": parsed.success,
        "mode": mode,
        "query": public_query,
        "localFilter": {
            "enabled": should_filter_locally,
            "month": month,
            "day": day,
        },
        "normalizedCostType": public_query["searchCostType"],
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
