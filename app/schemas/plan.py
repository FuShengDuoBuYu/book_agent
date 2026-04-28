from typing import Literal

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
# 这个是Agent在规划层可以调用的tools的名单
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

# 调用工具的一条计划记录
class ToolCall(BaseModel):
    id: str = ""
    # toolName必须是ToolName里面的一个, 目前我们先写死一个工具, 后续如果有新的工具, 可以继续往ToolName里面添加
    toolName: ToolName = "search_personal_orders"
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
