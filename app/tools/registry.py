from app.schemas.plan import SearchPersonalOrdersArgs


PLANNER_TOOL_BY_NAME = {
    "search_personal_orders": "查询当前账号的个人账单。",
    "search_family_orders": "查询当前账号有权访问的家庭账单。",
}


def render_planner_tools_for_prompt() -> str:
    tools = [f"- {name}: {description}" for name, description in PLANNER_TOOL_BY_NAME.items()]
    fields = [
        f"  - {name}: {field.description}"
        for name, field in SearchPersonalOrdersArgs.model_fields.items()
    ]
    return "\n".join([*tools, "查询过滤条件", *fields])
