import json
from collections import defaultdict
from typing import Any

from langsmith import traceable


@traceable(name="calculate_order_summary", run_type="tool")
def calculate_order_summary(orders_json: str) -> str:
    # summary 是所有分析里最基础的一层：先把总笔数、总收入、总支出这些基础指标算出来。
    orders = _extract_orders(orders_json)
    expense_orders = [order for order in orders if _is_expense_order(order)]
    income_orders = [order for order in orders if _is_income_order(order)]

    expense_total = _round_money(sum(_amount(order) for order in expense_orders))
    income_total = _round_money(sum(_amount(order) for order in income_orders))

    return _to_json(
        {
            "orderCount": len(orders),
            "expenseOrderCount": len(expense_orders),
            "incomeOrderCount": len(income_orders),
            "expenseTotal": expense_total,
            "incomeTotal": income_total,
            "netIncomeMinusExpense": _round_money(income_total - expense_total),
            "averageExpense": _safe_average(expense_total, len(expense_orders)),
            "averageIncome": _safe_average(income_total, len(income_orders)),
            "dateRange": _date_range(orders),
            "expenseDays": _unique_day_count(expense_orders),
            "incomeDays": _unique_day_count(income_orders),
        }
    )


@traceable(name="calculate_category_breakdown", run_type="tool")
def calculate_category_breakdown(orders_json: str) -> str:
    orders = _extract_orders(orders_json)
    expense_orders = [order for order in orders if _is_expense_order(order)]
    income_orders = [order for order in orders if _is_income_order(order)]
    expense_total = sum(_amount(order) for order in expense_orders)
    income_total = sum(_amount(order) for order in income_orders)

    return _to_json(
        {
            "expenseCategories": _group_amounts(
                expense_orders,
                key_fn=lambda order: str(order.get("costType") or "未分类"),
                denominator=expense_total,
            ),
            "incomeCategories": _group_amounts(
                income_orders,
                key_fn=lambda order: str(order.get("costType") or "收入"),
                denominator=income_total,
            ),
        }
    )


@traceable(name="find_top_expenses", run_type="tool")
def find_top_expenses(orders_json: str, top_n: int = 5) -> str:
    orders = _extract_orders(orders_json)
    expense_orders = [order for order in orders if _is_expense_order(order)]
    sorted_orders = sorted(expense_orders, key=_amount, reverse=True)
    return _to_json(
        {
            "topN": top_n,
            "orders": [_compact_order(order) for order in sorted_orders[:top_n]],
            "maxExpense": _compact_order(sorted_orders[0]) if sorted_orders else None,
        }
    )


@traceable(name="find_top_incomes", run_type="tool")
def find_top_incomes(orders_json: str, top_n: int = 5) -> str:
    orders = _extract_orders(orders_json)
    income_orders = [order for order in orders if _is_income_order(order)]
    sorted_orders = sorted(income_orders, key=_amount, reverse=True)
    return _to_json(
        {
            "topN": top_n,
            "orders": [_compact_order(order) for order in sorted_orders[:top_n]],
            "maxIncome": _compact_order(sorted_orders[0]) if sorted_orders else None,
        }
    )


@traceable(name="calculate_daily_breakdown", run_type="tool")
def calculate_daily_breakdown(orders_json: str) -> str:
    orders = _extract_orders(orders_json)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        buckets[_date_key(order)].append(order)

    rows = []
    for date_key, items in buckets.items():
        expense_orders = [order for order in items if _is_expense_order(order)]
        income_orders = [order for order in items if _is_income_order(order)]
        expense_total = sum(_amount(order) for order in expense_orders)
        income_total = sum(_amount(order) for order in income_orders)
        rows.append(
            {
                "date": date_key,
                "orderCount": len(items),
                "expenseOrderCount": len(expense_orders),
                "incomeOrderCount": len(income_orders),
                "expenseTotal": _round_money(expense_total),
                "incomeTotal": _round_money(income_total),
                "netIncomeMinusExpense": _round_money(income_total - expense_total),
            }
        )

    rows.sort(key=lambda item: item["date"])
    return _to_json({"days": rows})


@traceable(name="calculate_monthly_breakdown", run_type="tool")
def calculate_monthly_breakdown(orders_json: str) -> str:
    orders = _extract_orders(orders_json)
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        buckets[_month_key(order)].append(order)

    rows = []
    for month_key, items in buckets.items():
        expense_orders = [order for order in items if _is_expense_order(order)]
        income_orders = [order for order in items if _is_income_order(order)]
        expense_total = sum(_amount(order) for order in expense_orders)
        income_total = sum(_amount(order) for order in income_orders)
        rows.append(
            {
                "month": month_key,
                "orderCount": len(items),
                "expenseTotal": _round_money(expense_total),
                "incomeTotal": _round_money(income_total),
                "netIncomeMinusExpense": _round_money(income_total - expense_total),
            }
        )

    rows.sort(key=lambda item: item["month"])
    return _to_json({"months": rows})


@traceable(name="calculate_payment_method_breakdown", run_type="tool")
def calculate_payment_method_breakdown(orders_json: str) -> str:
    orders = _extract_orders(orders_json)
    expense_orders = [order for order in orders if _is_expense_order(order)]
    income_orders = [order for order in orders if _is_income_order(order)]
    expense_total = sum(_amount(order) for order in expense_orders)
    income_total = sum(_amount(order) for order in income_orders)

    return _to_json(
        {
            "expensePaymentMethods": _group_amounts(
                expense_orders,
                key_fn=lambda order: str(order.get("bankName") or "未知支付方式"),
                denominator=expense_total,
            ),
            "incomePaymentMethods": _group_amounts(
                income_orders,
                key_fn=lambda order: str(order.get("bankName") or "未知来源"),
                denominator=income_total,
            ),
        }
    )


@traceable(name="calculate_remark_breakdown", run_type="tool")
def calculate_remark_breakdown(orders_json: str) -> str:
    orders = _extract_orders(orders_json)
    expense_orders = [order for order in orders if _is_expense_order(order)]
    expense_total = sum(_amount(order) for order in expense_orders)
    return _to_json(
        {
            "expenseRemarks": _group_amounts(
                expense_orders,
                key_fn=lambda order: str(order.get("orderRemark") or "无备注"),
                denominator=expense_total,
            )
        }
    )

@traceable(name="compare_periods", run_type="tool")
def compare_periods(periods_json: str) -> str:
    periods = _extract_periods(periods_json)
    rows = []
    for period in periods:
        label = str(period.get("label") or period.get("summary") or "未命名周期")
        analysis = period.get("calculationToolResults") or {}
        summary = analysis.get("summary", {})
        categories = analysis.get("categoryBreakdown", {}).get("expenseCategories", [])
        rows.append(
            {
                "label": label,
                "orderCount": summary.get("orderCount", 0),
                "expenseTotal": summary.get("expenseTotal", 0),
                "incomeTotal": summary.get("incomeTotal", 0),
                "netIncomeMinusExpense": summary.get("netIncomeMinusExpense", 0),
                "topExpense": analysis.get("topExpenses", {}).get("maxExpense"),
                "topExpenseCategory": _compact_category(categories[0]) if categories else None,
                "topExpenseCategories": [_compact_category(item) for item in categories[:5]],
            }
        )

    largest_expense_period = _max_row(rows, "expenseTotal")
    largest_income_period = _max_row(rows, "incomeTotal")
    return _to_json(
        {
            "periods": rows,
            "expenseTotalRanking": [
                _ranking_row(item, "expenseTotal")
                for item in sorted(
                    rows, key=lambda item: item.get("expenseTotal", 0), reverse=True
                )
            ],
            "incomeTotalRanking": [
                _ranking_row(item, "incomeTotal")
                for item in sorted(
                    rows, key=lambda item: item.get("incomeTotal", 0), reverse=True
                )
            ],
            "largestExpensePeriod": _ranking_row(largest_expense_period, "expenseTotal"),
            "largestIncomePeriod": _ranking_row(largest_income_period, "incomeTotal"),
            "periodCount": len(rows),
        }
    )


def _extract_orders(value: str | list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    # 这个 helper 的作用是把各种来源的账单数据收口成统一 list[dict] 结构。
    # 因为不同链路传进来的可能是 JSON 字符串、原始数组，或者包着 result/ordersInfo 的对象。
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        orders = value.get("ordersInfo")
        if isinstance(orders, list):
            return [item for item in orders if isinstance(item, dict)]
        result = value.get("result")
        if isinstance(result, dict):
            return _extract_orders(result)
    return []


def _extract_periods(value: str | list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        periods = value.get("periods") or value.get("toolResults") or value.get("observations")
        if isinstance(periods, list):
            return [item for item in periods if isinstance(item, dict)]
    return []


def _is_income_order(order: dict[str, Any]) -> bool:
    # Android's manual entry and notification parser both store signed money.
    return _money(order) > 0


def _is_expense_order(order: dict[str, Any]) -> bool:
    return _money(order) < 0


def _group_amounts(
    orders: list[dict[str, Any]],
    key_fn,
    denominator: float,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for order in orders:
        key = key_fn(order)
        if key not in buckets:
            buckets[key] = {"name": key, "amount": 0.0, "orderCount": 0}
        buckets[key]["amount"] += _amount(order)
        buckets[key]["orderCount"] += 1

    rows = []
    for item in buckets.values():
        amount = _round_money(item["amount"])
        rows.append(
            {
                "name": item["name"],
                "amount": amount,
                "orderCount": item["orderCount"],
                "ratio": _safe_ratio(amount, denominator),
                "ratioPercent": _round_money(_safe_ratio(amount, denominator) * 100),
            }
        )
    rows.sort(key=lambda item: item["amount"], reverse=True)
    return rows

# 做基本数据清洗和格式化，保证输出的订单数据结构统一且字段齐全，方便后续分析工具使用。
def _compact_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "amount": _round_money(_amount(order)),
        "rawMoney": _round_money(_money(order)),
        "year": order.get("year"),
        "month": order.get("month"),
        "day": order.get("day"),
        "date": _date_key(order),
        "clock": order.get("clock", ""),
        "costType": order.get("costType", ""),
        "orderRemark": order.get("orderRemark", ""),
        "bankName": order.get("bankName", ""),
    }


def _date_key(order: dict[str, Any]) -> str:
    return (
        f"{int(order.get('year') or 0):04d}-"
        f"{int(order.get('month') or 0):02d}-"
        f"{int(order.get('day') or 0):02d}"
    )


def _month_key(order: dict[str, Any]) -> str:
    return f"{int(order.get('year') or 0):04d}-{int(order.get('month') or 0):02d}"


def _date_range(orders: list[dict[str, Any]]) -> dict[str, str | None]:
    if not orders:
        return {"start": None, "end": None}
    dates = sorted(_date_key(order) for order in orders)
    return {"start": dates[0], "end": dates[-1]}


def _unique_day_count(orders: list[dict[str, Any]]) -> int:
    return len({_date_key(order) for order in orders})


def _money(order: dict[str, Any]) -> float:
    try:
        return float(order.get("money") or 0)
    except (TypeError, ValueError):
        return 0.0


def _amount(order: dict[str, Any]) -> float:
    return abs(_money(order))


def _safe_average(total: float, count: int) -> float:
    return _round_money(total / count) if count else 0.0


def _safe_ratio(amount: float, denominator: float) -> float:
    return round(amount / denominator, 4) if denominator else 0.0


def _round_money(value: float) -> float:
    return round(float(value or 0), 2)


def _max_row(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda item: item.get(key, 0) or 0)


def _compact_category(category: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": category.get("name", ""),
        "amount": category.get("amount", 0),
        "orderCount": category.get("orderCount", 0),
        "ratioPercent": category.get("ratioPercent", 0),
    }


def _ranking_row(row: dict[str, Any] | None, metric: str) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "label": row.get("label", ""),
        metric: row.get(metric, 0),
    }


def _to_json(value: dict[str, Any] | list[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
