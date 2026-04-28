import json
from collections import defaultdict
from typing import Any

from langchain_core.tools import StructuredTool
from langsmith import traceable
from pydantic import BaseModel, Field


class OrdersJsonInput(BaseModel):
    orders_json: str = Field(description="账单 JSON 字符串，支持 ordersInfo 数组或原始账单数组")


class TopOrdersInput(OrdersJsonInput):
    top_n: int = Field(default=5, ge=1, le=50, description="返回前 N 笔")


class ComparePeriodsInput(BaseModel):
    periods_json: str = Field(
        description=(
            "多个周期的 JSON 字符串。格式为数组，每项包含 label 和 ordersInfo，"
            "也可以包含 calculationToolResults。"
        )
    )


@traceable(name="calculate_order_summary", run_type="tool")
def calculate_order_summary(orders_json: str) -> str:
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


@traceable(name="calculate_order_statistics_suite", run_type="tool")
def calculate_order_statistics_suite(orders_json: str) -> str:
    summary = json.loads(calculate_order_summary(orders_json))
    category_breakdown = json.loads(calculate_category_breakdown(orders_json))
    top_expenses = json.loads(find_top_expenses(orders_json, top_n=10))
    top_incomes = json.loads(find_top_incomes(orders_json, top_n=10))
    daily_breakdown = json.loads(calculate_daily_breakdown(orders_json))
    monthly_breakdown = json.loads(calculate_monthly_breakdown(orders_json))
    payment_breakdown = json.loads(calculate_payment_method_breakdown(orders_json))
    remark_breakdown = json.loads(calculate_remark_breakdown(orders_json))

    return _to_json(
        {
            "summary": summary,
            "categoryBreakdown": category_breakdown,
            "topExpenses": top_expenses,
            "topIncomes": top_incomes,
            "dailyBreakdown": daily_breakdown,
            "monthlyBreakdown": monthly_breakdown,
            "paymentMethodBreakdown": payment_breakdown,
            "remarkBreakdown": remark_breakdown,
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
                "topExpenseCategory": categories[0] if categories else None,
                "expenseCategories": categories,
            }
        )

    return _to_json(
        {
            "periods": rows,
            "expenseTotalRanking": sorted(
                rows, key=lambda item: item.get("expenseTotal", 0), reverse=True
            ),
            "incomeTotalRanking": sorted(
                rows, key=lambda item: item.get("incomeTotal", 0), reverse=True
            ),
            "largestExpensePeriod": _max_row(rows, "expenseTotal"),
            "largestIncomePeriod": _max_row(rows, "incomeTotal"),
            "periodCount": len(rows),
        }
    )


def _extract_orders(value: str | list[dict[str, Any]] | dict[str, Any]) -> list[dict[str, Any]]:
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
    money = _money(order)
    cost_type = str(order.get("costType") or "")
    remark = str(order.get("orderRemark") or "")
    income_keywords = ("收入", "工资", "奖金", "补贴", "报销", "退款", "转入", "到账")
    expense_keywords = (
        "饮食",
        "餐饮",
        "娱乐",
        "交通",
        "购物",
        "房租",
        "水电",
        "日用",
        "医疗",
        "学习",
        "聚会",
        "其他",
    )

    if any(keyword in cost_type for keyword in income_keywords):
        return True
    if any(keyword in cost_type for keyword in expense_keywords):
        return False
    if any(keyword in remark for keyword in income_keywords):
        return True
    return money > 0 and not cost_type


def _is_expense_order(order: dict[str, Any]) -> bool:
    return not _is_income_order(order)


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


def _to_json(value: dict[str, Any] | list[Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


calculate_order_summary_tool = StructuredTool.from_function(
    func=calculate_order_summary,
    name="calculate_order_summary",
    description="计算账单总支出、总收入、净收入、笔数、平均金额和日期范围。",
    args_schema=OrdersJsonInput,
)
calculate_category_breakdown_tool = StructuredTool.from_function(
    func=calculate_category_breakdown,
    name="calculate_category_breakdown",
    description="按账单分类计算金额、笔数、占比。",
    args_schema=OrdersJsonInput,
)
find_top_expenses_tool = StructuredTool.from_function(
    func=find_top_expenses,
    name="find_top_expenses",
    description="找出金额最大的 N 笔支出。",
    args_schema=TopOrdersInput,
)
find_top_incomes_tool = StructuredTool.from_function(
    func=find_top_incomes,
    name="find_top_incomes",
    description="找出金额最大的 N 笔收入。",
    args_schema=TopOrdersInput,
)
calculate_daily_breakdown_tool = StructuredTool.from_function(
    func=calculate_daily_breakdown,
    name="calculate_daily_breakdown",
    description="按天统计支出、收入、净额和笔数。",
    args_schema=OrdersJsonInput,
)
calculate_monthly_breakdown_tool = StructuredTool.from_function(
    func=calculate_monthly_breakdown,
    name="calculate_monthly_breakdown",
    description="按月统计支出、收入、净额和笔数。",
    args_schema=OrdersJsonInput,
)
calculate_payment_method_breakdown_tool = StructuredTool.from_function(
    func=calculate_payment_method_breakdown,
    name="calculate_payment_method_breakdown",
    description="按支付方式统计支出和收入金额、笔数、占比。",
    args_schema=OrdersJsonInput,
)
calculate_remark_breakdown_tool = StructuredTool.from_function(
    func=calculate_remark_breakdown,
    name="calculate_remark_breakdown",
    description="按备注统计支出金额、笔数、占比。",
    args_schema=OrdersJsonInput,
)
calculate_order_statistics_suite_tool = StructuredTool.from_function(
    func=calculate_order_statistics_suite,
    name="calculate_order_statistics_suite",
    description="一次性计算账单总览、分类、Top 支出、Top 收入、按天、按月、支付方式和备注统计。",
    args_schema=OrdersJsonInput,
)
compare_periods_tool = StructuredTool.from_function(
    func=compare_periods,
    name="compare_periods",
    description="对比多个周期的总支出、总收入、最高支出和分类结构。",
    args_schema=ComparePeriodsInput,
)
