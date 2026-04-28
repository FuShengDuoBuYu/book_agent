from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    # ...代表该字段为必填
    phoneNum: str = Field(..., min_length=1, description="User phone number")
    message: str = Field(..., min_length=1, description="User message")
    sessionId: str | None = Field(default=None, description="Conversation session id")
    mode: Literal["个人版", "家庭版"] = Field(
        default="个人版", description="Bookkeeping mode"
    )
    familyId: str | None = Field(
        default=None, description="Family id, required for family mode when available"
    )


class ChatResponse(BaseModel):
    reply: str
    # 利用sessionId来区分同一个用户的不同对话
    sessionId: str | None = None
