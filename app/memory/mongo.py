from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings


_client: AsyncIOMotorClient | None = None


def get_mongo_client() -> AsyncIOMotorClient:
    global _client

    if _client is None:
        settings = get_settings()
        if not settings.agent_mongodb_uri:
            raise RuntimeError("AGENT_MONGODB_URI is not configured")

        _client = AsyncIOMotorClient(settings.agent_mongodb_uri)

    return _client


def get_memory_db():
    settings = get_settings()
    return get_mongo_client()[settings.agent_mongodb_db]
