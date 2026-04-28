import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.agent.book_agent import BookAgent
from app.schemas.chat import ChatRequest, ChatResponse


router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    # 这个接口是“一次性拿最终答案”的普通 HTTP 模式。
    agent = BookAgent()

    try:
        reply, session_id = await agent.chat(
            phone_num=request.phoneNum,
            message=request.message,
            session_id=request.sessionId,
            mode=request.mode,
            family_id=request.familyId,
        )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return ChatResponse(reply=reply, sessionId=session_id)


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    # 这个接口走 SSE 流式输出，前端能边收状态边收模型增量文本。
    agent = BookAgent()

    async def event_stream():
        async for event in agent.stream_chat(
            phone_num=request.phoneNum,
            message=request.message,
            session_id=request.sessionId,
            mode=request.mode,
            family_id=request.familyId,
        ):
            # SSE 协议要求每个事件都以 data: 开头，并以空行结束。
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # 禁用中间层缓冲，否则前端会很晚才看到流式内容。
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
