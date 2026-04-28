from fastapi import FastAPI

from app.api.routes import chat, frontend, health
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version)

    app.include_router(health.router)
    app.include_router(chat.router, prefix="/api")
    frontend.register_frontend(app)
    app.include_router(frontend.router)

    return app


app = create_app()
