"""Evidence-bound prose; financial quantities are resolved from cited references."""

from pydantic import BaseModel, ConfigDict, Field


class ReportStatement(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=1600,
                      description='面向用户的简短判断或解释；数量用{{vN}}，程序判断用{{jN}}。')
    factIds: list[str] = Field(min_length=1, max_length=8,
                              description='支持本句的准确证据编号，不得跨账本借用数值。')


class ReportDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    conclusion: ReportStatement
    reasons: list[ReportStatement] = Field(default_factory=list, max_length=4)
    nextStep: ReportStatement | None = None
    caveat: ReportStatement | None = None
