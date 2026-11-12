import json
from pydantic import BaseModel, Field, field_validator


class ReportSection(BaseModel):
    title: str = Field(max_length=80)
    factIds: list[str] = Field(min_length=1, max_length=8)
    analysis: str = Field(max_length=1200)


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
