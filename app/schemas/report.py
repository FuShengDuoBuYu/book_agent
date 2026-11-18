import json
from pydantic import BaseModel, Field, field_validator


class ReportSection(BaseModel):
    title: str = Field(max_length=120, description='简洁标题；判断必须符合所引用证据')
    factIds: list[str] = Field(min_length=1, max_length=8, description='引用证据编号，程序会显示完整证据句和其中的数量')
    analysis: str = Field(max_length=1600, description='直接回答本轮目标，解释证据与条件，需要数量时使用 values 提供的 {{v1}} 引用，由程序填入真实值。表格由 artifacts 渲染，不在此手抄表格。')


class ReportDraft(BaseModel):
    sections: list[ReportSection] = Field(min_length=1, max_length=6)


class ReportReview(BaseModel):
    supported: bool
    completed: bool = True
    issues: list[str] = Field(default_factory=list, max_length=8)

    @field_validator('issues', mode='before')
    @classmethod
    def normalize_issue_details(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item for item in value]
        return value
