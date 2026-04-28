from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    phoneNum: str = Field(..., min_length=1, description="User phone number")
    message: str = Field(..., min_length=1, description="User message")
    sessionId: str | None = Field(default=None, description="Conversation session id")


class ChatResponse(BaseModel):
    reply: str
    sessionId: str | None = None
