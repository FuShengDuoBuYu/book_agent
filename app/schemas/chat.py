from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    phoneNum: str = Field(..., min_length=1, description="User phone number")
    message: str = Field(..., min_length=1, description="User message")


class ChatResponse(BaseModel):
    reply: str
