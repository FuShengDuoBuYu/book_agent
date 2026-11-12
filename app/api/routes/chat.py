import json

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse

from app.agent.book_agent import BookAgent
from app.api.routes.session import NO_STORE, LedgerSession, SessionDependency
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


def _chat_args(request: ChatRequest, session: LedgerSession) -> dict:
    family_id = session.account.familyId
    if request.mode == "家庭版" and not family_id:
        raise HTTPException(403, "当前账号未加入家庭账本。", headers=NO_STORE)
    return {
        "phone_num": session.account.phoneNum,
        "message": request.message,
        "session_id": request.sessionId,
        "mode": request.mode,
        "family_id": family_id,
    }


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest, session: SessionDependency, response: Response
) -> ChatResponse:
    args = _chat_args(request, session)
    try:
        reply, session_id = await BookAgent(session.client).chat(**args)
    except ConnectionError:
        raise HTTPException(503, "AI 账单服务暂不可用。", headers=NO_STORE) from None
    except RuntimeError:
        raise HTTPException(502, "AI 账单处理失败，请稍后重试。", headers=NO_STORE) from None

    response.headers.update(NO_STORE)
    return ChatResponse(reply=reply, sessionId=session_id)


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest, session: SessionDependency) -> StreamingResponse:
    args = _chat_args(request, session)
    agent = BookAgent(session.client)

    async def event_stream():
        async for event in agent.stream_chat(**args):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={**NO_STORE, "X-Accel-Buffering": "no"},
    )
