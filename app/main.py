from fastapi import FastAPI

from app.api.routes import chat, frontend, health
from app.core.config import get_settings


def create_app() -> FastAPI:
    # 应用入口只负责组装依赖和注册路由，真正的业务逻辑下沉到各模块中。
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version)

    # 健康检查通常最先注册，便于部署环境快速探活。
    app.include_router(health.router)
    # 对话相关接口统一挂到 /api 前缀下，前端通过这里进入 Agent 主流程。
    app.include_router(chat.router, prefix="/api")
    # 前端静态资源和页面路由单独注册，方便本地直接跑一个完整应用。
    frontend.register_frontend(app)
    app.include_router(frontend.router)

    return app


app = create_app()
