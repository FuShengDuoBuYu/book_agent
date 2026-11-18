import json
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReportSection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(max_length=120, description='简洁标题；判断必须符合所引用证据')
    factIds: list[str] = Field(min_length=1, max_length=8, description='引用证据编号，程序会显示完整证据句和其中的数量')
    analysis: str = Field(max_length=1600, description='直接回答本轮目标，数量仅引用本段 factIds 的相同指标和单位；或用已含单位的 {{v1}} 变量。不得换算或手抄表格，表格由 artifacts 渲染。')


class ReportDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sections: list[ReportSection] = Field(min_length=1, max_length=6)


class ReportReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    supported: bool
    completed: bool
    issues: list[str] = Field(default_factory=list, max_length=8)

    @field_validator('issues', mode='before')
    @classmethod
    def normalize_issue_details(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item for item in value]
        return value
