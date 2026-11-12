import json
from pydantic import BaseModel, Field, field_validator


class ReportSection(BaseModel):
    title: str = Field(max_length=80, description='不带数字的标题；判断必须符合所引用证据')
    factIds: list[str] = Field(min_length=1, max_length=8, description='引用证据编号，程序会显示完整证据句和其中的数量')
    analysis: str = Field(max_length=1200, description='只写不带数字、不复述金额比例期限的含义和下一步。所有数量通过factIds显示。例：先核对必付账单，再评估新增收入的可实现性。')


class ReportDraft(BaseModel):
    sections: list[ReportSection] = Field(min_length=1, max_length=6)


class ReportReview(BaseModel):
    supported: bool
    issues: list[str] = Field(default_factory=list, max_length=8)

    @field_validator('issues', mode='before')
    @classmethod
    def normalize_issue_details(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item for item in value]
        return value
