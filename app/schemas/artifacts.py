"""Public, inert analysis data; never expose internal message metadata."""

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Column(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(max_length=120)
    unit: str = Field(default='', max_length=20)


class Series(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(max_length=120)


class AnalysisArtifact(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=80)
    kind: Literal['table', 'line', 'bar']
    presentation: Literal['supporting', 'requested'] | None = None
    title: str = Field(max_length=160)
    columns: list[Column] = Field(min_length=1, max_length=16)
    rows: list[dict] = Field(max_length=1000)
    xKey: str | None = None
    series: list[Series] = Field(default_factory=list, max_length=8)
    notes: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode='after')
    def validate_data(self):
        keys = {column.key for column in self.columns}
        if len(keys) != len(self.columns):
            raise ValueError('Duplicate artifact column')
        if self.kind != 'table' and (self.xKey not in keys or not self.series):
            raise ValueError('Chart requires an axis and series')
        if any(series.key not in keys for series in self.series):
            raise ValueError('Unknown series column')
        for row in self.rows:
            if set(row) - keys:
                raise ValueError('Unpublished artifact fields')
            for value in row.values():
                if value is not None and not isinstance(value, (str, bool, int, float)):
                    raise ValueError('Artifact cells must be primitive values')
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError('Artifact amounts must be finite')
                if isinstance(value, str) and len(value) > 2000:
                    raise ValueError('Artifact cell too long')
            for series in self.series:
                value = row.get(series.key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    raise ValueError('Chart series must be numeric or unknown')
        return self


def public_artifacts(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    result = []
    for row in value[:12]:
        try:
            result.append(AnalysisArtifact.model_validate(row).model_dump(exclude_none=True))
        except (ValueError, TypeError):
            continue
    return result
