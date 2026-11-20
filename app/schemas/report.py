from pydantic import BaseModel, ConfigDict, Field


class ReportSection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(max_length=120, description='简洁标题；判断必须符合所引用证据')
    factIds: list[str] = Field(min_length=1, max_length=8, description='引用证据编号，程序会显示完整证据句和其中的数量')
    analysis: str = Field(max_length=12000, description='由程序根据本段绑定的计算结果生成的分析发现、限制与行动建议')


class ReportDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sections: list[ReportSection] = Field(min_length=1, max_length=6)
