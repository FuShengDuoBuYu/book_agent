from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    sessionId: str | None = None
    mode: Literal["个人版", "家庭版"] = "个人版"


class ChatResponse(BaseModel):
    reply: str
    sessionId: str | None = None
