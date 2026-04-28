from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings


router = APIRouter(tags=["frontend"])


def register_frontend(app: FastAPI) -> None:
    settings = get_settings()
    assets_dir = settings.frontend_dist / "assets"

    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@router.get("/")
async def index() -> FileResponse:
    return _frontend_index()


@router.get("/{path:path}")
async def spa_fallback(path: str) -> FileResponse:
    return _frontend_index()


def _frontend_index() -> FileResponse:
    settings = get_settings()
    index_file = settings.frontend_dist / "index.html"

    if not index_file.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "前端尚未构建。开发时请进入 frontend 运行 npm run dev；"
                "或运行 npm run build 后再访问后端服务。"
            ),
        )

    return FileResponse(Path(index_file))
