from fastapi import APIRouter, HTTPException

from app.agent.book_agent import BookAgent
from app.schemas.chat import ChatRequest, ChatResponse


router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    agent = BookAgent()

    try:
        reply = await agent.chat(phone_num=request.phoneNum, message=request.message)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(reply=reply)
