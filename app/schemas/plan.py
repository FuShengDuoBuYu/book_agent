from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
IntentType = Literal["analyze_orders", "chat", "unknown"]
ToolName = Literal["search_personal_orders"]


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


class ToolCall(BaseModel):
    id: str = ""
    toolName: ToolName = "search_personal_orders"
    args: SearchPersonalOrdersArgs = Field(default_factory=SearchPersonalOrdersArgs)
    reason: str = ""


class AgentPlan(BaseModel):
    intent: IntentType = "analyze_orders"
    analysisType: AnalysisType = "general_summary"
    needsTools: bool = True
    toolCalls: list[ToolCall] = Field(default_factory=list)
    finalInstruction: str = "根据工具结果回答用户问题。"
    followUpOfPreviousQuestion: bool = False
    summary: str = "本次账单分析计划"
    reason: str = ""
