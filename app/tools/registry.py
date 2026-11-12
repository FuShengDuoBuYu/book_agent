from app.schemas.plan import SearchPersonalOrdersArgs, ToolCall


PLANNER_TOOL_BY_NAME = {
    "search_personal_orders": ("个人版", "查询当前账号的个人账单。"),
    "search_family_orders": ("家庭版", "查询当前账号有权访问的家庭账单。"),
}


def render_planner_tools_for_prompt() -> str:
    tools = [
        f"- {name}: {description}"
        for name, (_, description) in PLANNER_TOOL_BY_NAME.items()
    ]
    fields = [
        f"  - {name}: {field.description}"
        for name, field in SearchPersonalOrdersArgs.model_fields.items()
    ]
    return "\n".join([*tools, "查询过滤条件", *fields])


def format_tool_call(tool_call: ToolCall) -> str:
    args = tool_call.args
    mode, _ = PLANNER_TOOL_BY_NAME.get(tool_call.toolName, ("", ""))
    scope = mode.removesuffix("版")
    if args.year is None:
        if args.month is not None and args.day is not None:
            return f"{args.month}月{args.day}日{scope}账单（不限年份）"
        if args.month is not None:
            return f"{args.month}月{scope}账单（不限年份）"
        return f"全部{scope}账单"
    if args.month is None:
        return f"{args.year}年{scope}账单"
    if args.day is None:
        return f"{args.year}年{args.month}月{scope}账单"
    return f"{args.year}年{args.month}月{args.day}日{scope}账单"
