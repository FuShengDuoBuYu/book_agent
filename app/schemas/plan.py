from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator



# Agent会在plan的时候先判断用户的意图,到底是分析账单还是chat还是unknown

IntentType = Literal["analyze_orders", "chat", "unknown"]
# 如果是分析账单,那么分析会有如下这几种不同的分析类型
AnalysisType = Literal[
    "highest_expense",
    "category_expense",
    "total_spending",
    "income_expense",
    "comparison",
    "general_summary",
    "chat",
    "unknown",
]


def literal_options(type_alias: object) -> tuple[str, ...]:
    return tuple(str(item) for item in get_args(type_alias))


def render_literal_options(type_alias: object) -> str:
    return " | ".join(f'"{item}"' for item in literal_options(type_alias))


def intent_options() -> tuple[str, ...]:
    return literal_options(IntentType)


def analysis_type_options() -> tuple[str, ...]:
    return literal_options(AnalysisType)


def render_intents_for_prompt() -> str:
    return render_literal_options(IntentType)


def render_analysis_types_for_prompt() -> str:
    return render_literal_options(AnalysisType)


class SearchPersonalOrdersArgs(BaseModel):
    year: int | None = None
    month: int | None = None
    day: int | None = None
    cost_type: str = "不限"
    remark: str = ""

    @field_validator("month")
    @classmethod
    def validate_month(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 12:
            raise ValueError("month must be between 1 and 12")
        return value

    @field_validator("day")
    @classmethod
    def validate_day(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 31:
            raise ValueError("day must be between 1 and 31")
        return value

# 调用工具的一条计划记录
class ToolCall(BaseModel):
    id: str = ""
    # toolName 的合法性由 app.tools.registry.PLANNER_TOOL_BY_NAME 动态校验，
    # schema 层不写死具体工具名，避免工具注册表和 schema 出现两份名单。
    toolName: str = ""
    args: SearchPersonalOrdersArgs = Field(default_factory=SearchPersonalOrdersArgs)
    # 模型认为调用这个工具的目的是什么
    reason: str = ""

# Agent plan的完整计划
class AgentPlan(BaseModel):
    # 用户的意图, 是分析账单还是聊天还是未知,默认是分析账单
    intent: IntentType = "analyze_orders"
    # 具体的分析类型, 如果用户的意图是分析账单, 那么就需要进一步判断分析的类型, 是分析最高消费还是分析各个类别的消费还是分析总消费还是分析收入支出还是分析同比环比还是一般总结, 默认是一般总结
    analysisType: AnalysisType = "general_summary"
    needsTools: bool = True
    # 可能要执行多次或者多个工具,如果有依赖关系,可能要加depends_on字段, 目前没有
    toolCalls: list[ToolCall] = Field(default_factory=list)
    finalInstruction: str = "根据工具结果回答用户问题。"
    followUpOfPreviousQuestion: bool = False
    summary: str = "本次账单分析计划"
    reason: str = ""
