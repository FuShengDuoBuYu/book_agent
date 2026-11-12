from datetime import date
from typing import Literal, get_args

from pydantic import AliasChoices, BaseModel, Field, FiniteFloat, field_validator, model_validator

from app.tools.financial_analysis import ANALYSIS_TOOLS



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
    "financial_analysis",
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
    year: int | None = Field(default=None, description="查询年份")
    month: int | None = Field(default=None, description="查询月份，1–12")
    day: int | None = Field(default=None, description="查询日期，1–31")
    cost_type: str = Field(default="不限", description="账单分类")
    remark: str = Field(default="", description="备注关键词")

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
    sourceIds: list[str] = Field(default_factory=list)
    # toolName 的合法性由 app.tools.registry.PLANNER_TOOL_BY_NAME 动态校验，
    # schema 层不写死具体工具名，避免工具注册表和 schema 出现两份名单。
    toolName: str = ""
    args: SearchPersonalOrdersArgs = Field(default_factory=SearchPersonalOrdersArgs)
    # 模型认为调用这个工具的目的是什么
    reason: str = ""

# Agent plan的完整计划
class ScenarioChange(BaseModel):
    metric: Literal["income", "expense"]
    amount: FiniteFloat
    count: float = Field(default=1, gt=0, le=100000, allow_inf_nan=False)
    label: str = Field(default="用户假设", max_length=200)


class AnalysisRequest(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def omit_unused_metric(cls, value):
        if isinstance(value, dict) and value.get("tool") != "forecast" and value.get("metric", "income") is None:
            value = dict(value)
            value.pop("metric")
        return value

    tool: str
    queryIds: list[str] = Field(default_factory=list, max_length=24)
    metric: Literal["income", "expense", "net"] = "income"
    targetMonth: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    changes: list[ScenarioChange] = Field(default_factory=list, max_length=20)
    budgetAmount: float | None = Field(default=None, ge=0, le=1e12, allow_inf_nan=False)
    startingBalance: FiniteFloat | None = None
    targetAmount: float | None = Field(default=None, ge=0, le=1e12, allow_inf_nan=False)
    months: int | None = Field(default=None, ge=1, le=600)

    @field_validator("tool")
    @classmethod
    def validate_tool(cls, value: str) -> str:
        if value not in ANALYSIS_TOOLS:
            raise ValueError("Unknown analysis tool")
        return value

    @field_validator("targetMonth")
    @classmethod
    def validate_target(cls, value: str | None) -> str | None:
        if value:
            date.fromisoformat(value + "-01")
        return value


class AnalysisWindow(BaseModel):
    startDate: date = Field(validation_alias=AliasChoices("startDate", "start"))
    endDate: date = Field(validation_alias=AliasChoices("endDate", "end"))

    @model_validator(mode="after")
    def validate_order(self):
        if self.endDate < self.startDate:
            raise ValueError("Analysis window ends before its start")
        return self


class AgentPlan(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def normalize_analysis_tool_alias(cls, value):
        if isinstance(value, dict) and value.get("analysisType") in ANALYSIS_TOOLS:
            value = dict(value)
            tool = value["analysisType"]
            value["analysisType"] = "financial_analysis"
            if not value.get("analyses"):
                value["analyses"] = [{"tool": tool}]
        return value

    # 用户的意图, 是分析账单还是聊天还是未知,默认是分析账单
    intent: IntentType = "analyze_orders"
    # 具体的分析类型, 如果用户的意图是分析账单, 那么就需要进一步判断分析的类型, 是分析最高消费还是分析各个类别的消费还是分析总消费还是分析收入支出还是分析同比环比还是一般总结, 默认是一般总结
    analysisType: AnalysisType = "general_summary"
    needsTools: bool = True
    # 可能要执行多次或者多个工具,如果有依赖关系,可能要加depends_on字段, 目前没有
    toolCalls: list[ToolCall] = Field(default_factory=list, max_length=24)
    analyses: list[AnalysisRequest] = Field(default_factory=list, max_length=12)
    analysisWindow: AnalysisWindow | None = None
    finalInstruction: str = "根据工具结果回答用户问题。"
    followUpOfPreviousQuestion: bool = False
    summary: str = "本次账单分析计划"
    reason: str = ""
