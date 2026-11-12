from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Response
from pymongo.errors import PyMongoError

from app.api.routes.session import NO_STORE, SessionDependency
from app.memory.store import MongoMemoryStore, conversation_scope

router = APIRouter(tags=["conversations"])
BookMode = Literal["个人版", "家庭版"]
Cursor = Annotated[str | None, Query(max_length=512)]


def _scope(session, mode: str) -> str:
    family_id = session.account.familyId
    if mode == "家庭版" and not family_id:
        raise HTTPException(403, "当前账号未加入家庭账本。", headers=NO_STORE)
    return conversation_scope(mode, family_id)


@router.get("/conversations")
async def list_conversations(session: SessionDependency, response: Response, mode: BookMode = "个人版",
                             limit: Annotated[int, Query(ge=1, le=100)] = 30, cursor: Cursor = None):
    scope = _scope(session, mode)
    response.headers.update(NO_STORE)
    try:
        return await MongoMemoryStore().list_sessions(
            phone_num=session.account.phoneNum, book_scope=scope, limit=limit, cursor=cursor)
    except ValueError:
        raise HTTPException(422, "历史记录分页参数无效。", headers=NO_STORE) from None
    except PyMongoError:
        raise HTTPException(503, "历史记录暂时无法读取，请稍后重试。", headers=NO_STORE) from None


@router.get("/conversations/{session_id}")
async def get_conversation(session_id: Annotated[str, Path(min_length=1, max_length=128)],
                           session: SessionDependency, response: Response, mode: BookMode = "个人版",
                           limit: Annotated[int, Query(ge=1, le=100)] = 50, cursor: Cursor = None):
    scope = _scope(session, mode)
    response.headers.update(NO_STORE)
    try:
        result = await MongoMemoryStore().get_session_messages(
            phone_num=session.account.phoneNum, book_scope=scope, session_id=session_id,
            limit=limit, cursor=cursor)
    except ValueError:
        raise HTTPException(422, "历史记录分页参数无效。", headers=NO_STORE) from None
    except PyMongoError:
        raise HTTPException(503, "历史记录暂时无法读取，请稍后重试。", headers=NO_STORE) from None
    if result is None:
        raise HTTPException(404, "当前账本中没有找到这段对话。", headers=NO_STORE)
    return result
