from langchain_core.tools import BaseTool

from app.tools.family_orders import search_family_orders_tool
from app.tools.order_calculations import (
    calculate_category_breakdown_tool,
    calculate_daily_breakdown_tool,
    calculate_monthly_breakdown_tool,
    calculate_order_summary_tool,
    calculate_payment_method_breakdown_tool,
    calculate_remark_breakdown_tool,
    compare_periods_tool,
    find_top_expenses_tool,
    find_top_incomes_tool,
)
from app.tools.personal_orders import search_personal_orders_tool


# 模型在 Planner 阶段可以选择的业务工具。
# 当前只开放“查个人账单 / 查家庭账单”，计算类工具由执行层内部决定何时调用。
PLANNER_TOOLS: list[BaseTool] = [
    search_personal_orders_tool,
    search_family_orders_tool,
]


# 内部确定性计算工具。它们用于压缩和分析查询结果，不让 Planner 直接选择，
# 避免模型在规划阶段把所有计算工具都调用一遍。
INTERNAL_CALCULATION_TOOLS: list[BaseTool] = [
    calculate_order_summary_tool,
    calculate_category_breakdown_tool,
    find_top_expenses_tool,
    find_top_incomes_tool,
    calculate_daily_breakdown_tool,
    calculate_monthly_breakdown_tool,
    calculate_payment_method_breakdown_tool,
    calculate_remark_breakdown_tool,
    compare_periods_tool,
]


PLANNER_TOOL_BY_NAME: dict[str, BaseTool] = {
    tool.name: tool for tool in PLANNER_TOOLS
}

INTERNAL_CALCULATION_TOOL_BY_NAME: dict[str, BaseTool] = {
    tool.name: tool for tool in INTERNAL_CALCULATION_TOOLS
}


def planner_tool_names() -> tuple[str, ...]:
    return tuple(PLANNER_TOOL_BY_NAME.keys())


def default_tool_name_for_mode(book_mode: str) -> str:
    return (
        "search_family_orders"
        if str(book_mode or "").strip() in {"家庭版", "家庭", "family", "family_mode"}
        else "search_personal_orders"
    )


def render_planner_tools_for_prompt() -> str:
    lines = []
    for tool in PLANNER_TOOLS:
        lines.append(f"- {tool.name}: {tool.description}")
        schema = getattr(tool, "args_schema", None)
        if schema:
            fields = getattr(schema, "model_fields", {})
            for field_name, field in fields.items():
                if field_name in {"phone_num", "family_id"}:
                    continue
                description = field.description or ""
                lines.append(f"  - {field_name}: {description}")
    return "\n".join(lines)
