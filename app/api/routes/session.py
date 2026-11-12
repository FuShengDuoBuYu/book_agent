from dataclasses import dataclass, field
from typing import Annotated

import httpx
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr

from app.clients.bookkeeping_api import BookkeepingApiClient

router = APIRouter(tags=["session"])
COOKIE_NAME = "__Host-book-agent"
NO_STORE = {"Cache-Control": "no-store"}
COOKIE_OPTIONS = {"path": "/", "secure": True, "httponly": True, "samesite": "strict"}


class TicketCredentials(BaseModel):
    phoneNum: str = Field(min_length=1, max_length=32)
    password: SecretStr = Field(min_length=1, max_length=128)


class LedgerTicket(BaseModel):
    ticket: str = Field(min_length=1, max_length=128)
    expiresIn: int = Field(gt=0, le=3600)


class LedgerGrant(BaseModel):
    token: str = Field(min_length=1, max_length=128)
    expiresIn: int = Field(gt=0, le=3600)


class LedgerAccount(BaseModel):
    phoneNum: str = Field(min_length=1)
    familyId: str = ""


@dataclass(frozen=True)
class LedgerSession:
    client: BookkeepingApiClient = field(repr=False)
    account: LedgerAccount = field(repr=False)


async def require_session(
    token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> LedgerSession:
    if not token or len(token) > 128:
        raise HTTPException(401, "请从自动记账重新进入 AI 账单。", headers=NO_STORE)
    client = BookkeepingApiClient(token)
    try:
        account = LedgerAccount.model_validate(await client.get_session())
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise HTTPException(401, "账本会话已失效，请从 App 重新进入。", headers=NO_STORE) from None
        raise HTTPException(503, "账本服务暂不可用。", headers=NO_STORE) from None
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "账本服务暂不可用。", headers=NO_STORE) from None
    return LedgerSession(client, account)


SessionDependency = Annotated[LedgerSession, Depends(require_session)]


@router.post("/session/ticket", response_model=LedgerTicket)
async def issue_ticket(request: Request, response: Response) -> LedgerTicket:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise HTTPException(413, "请求体过大。", headers=NO_STORE)
        body.extend(chunk)
    try:
        credentials = TicketCredentials.model_validate_json(body)
    except ValueError:
        raise HTTPException(401, "账号或密码验证失败。", headers=NO_STORE) from None

    try:
        ticket = LedgerTicket.model_validate(await BookkeepingApiClient().issue_ticket(
            credentials.phoneNum, credentials.password.get_secret_value()
        ))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {400, 401, 403, 422}:
            raise HTTPException(401, "账号或密码验证失败。", headers=NO_STORE) from None
        raise HTTPException(503, "账本服务暂不可用。", headers=NO_STORE) from None
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "账本服务暂不可用。", headers=NO_STORE) from None
    response.headers.update(NO_STORE)
    return ticket


@router.post("/session/exchange")
async def exchange_ticket(ticket: Annotated[str, Form()] = "") -> Response:
    if not ticket or len(ticket) > 128:
        return _launch_error(401, "请从自动记账重新进入 AI 账单。")

    try:
        grant = LedgerGrant.model_validate(await BookkeepingApiClient().exchange_ticket(ticket))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {400, 401, 403}:
            return _launch_error(401, "登录票据已失效，请从 App 重新进入。")
        return _launch_error(503, "账本服务暂不可用，请稍后从 App 重试。")
    except (httpx.HTTPError, ValueError):
        return _launch_error(503, "账本服务暂不可用，请稍后从 App 重试。")

    response = RedirectResponse("/", status_code=303, headers=NO_STORE)
    response.set_cookie(COOKIE_NAME, grant.token, max_age=grant.expiresIn, **COOKIE_OPTIONS)
    return response


def _launch_error(status_code: int, message: str) -> Response:
    response = PlainTextResponse(message, status_code=status_code, headers=NO_STORE)
    response.delete_cookie(COOKIE_NAME, **COOKIE_OPTIONS)
    return response


@router.get("/session")
async def session_status(session: SessionDependency, response: Response) -> dict[str, bool]:
    response.headers.update(NO_STORE)
    return {"familyAvailable": bool(session.account.familyId)}
