"""Process and bounded model-gateway readiness checks."""

import asyncio
from hashlib import sha256
from time import monotonic

import httpx
from fastapi import APIRouter, Response

from app.core.config import get_settings

router = APIRouter(tags=['health'])


class GatewayProbe:
    """Share in-flight probes and briefly cache only successful configurations."""

    def __init__(self):
        self._key = None
        self._success = None
        self._expires = 0
        self._inflight = {}

    async def check(self, settings):
        native = settings.llm_provider == 'ollama'
        secret = settings.llm_api_key.get_secret_value() if settings.llm_api_key and not native else ''
        key = (settings.llm_provider, settings.llm_base_url, settings.llm_model,
               sha256(secret.encode()).digest())
        if key == self._key and monotonic() < self._expires:
            return dict(self._success)
        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._probe(settings.llm_provider, settings.llm_base_url,
                                                   settings.llm_model, secret))
            self._inflight[key] = task
            task.add_done_callback(lambda completed: self._complete(key, completed))
        # One disconnected monitor must not cancel the other monitors' probe.
        return dict(await asyncio.shield(task))

    def _complete(self, key, task):
        if self._inflight.get(key) is task:
            self._inflight.pop(key)
        if task.cancelled() or task.exception() is not None:
            return
        result = task.result()
        if result['status'] == 'ok':
            self._key, self._success, self._expires = key, result, monotonic() + 30

    async def _probe(self, provider, base_url, model, secret):
        native = provider == 'ollama'
        path, list_key = ('/api/tags', 'models') if native else ('/models', 'data')
        name_key = 'name' if native else 'id'
        headers = {'Authorization': f'Bearer {secret}'} if secret else {}
        try:
            # Official API TLS/proxy cold connections can exceed the local five seconds.
            timeout = 20 if provider == 'openai' else 5
            async with httpx.AsyncClient(timeout=timeout) as client:
                result = await client.get(f'{base_url}{path}', headers=headers)
                result.raise_for_status()
            models = result.json()[list_key]
            found = any(item.get(name_key) == model for item in models)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return {'status': 'model_gateway_unavailable'}
        if not found:
            return {'status': 'model_not_listed'}
        return {'status': 'ok', 'provider': provider, 'model': model}


gateway_probe = GatewayProbe()


@router.get('/health')
async def health() -> dict[str, str]:
    return {'service': 'ok'}


@router.get('/ready')
async def ready(response: Response) -> dict[str, str]:
    response.headers['Cache-Control'] = 'no-store'
    settings = get_settings()
    if not settings.agent_mongodb_uri:
        response.status_code = 503
        return {'status': 'memory_not_configured'}
    result = await gateway_probe.check(settings)
    if result['status'] != 'ok':
        response.status_code = 503
    return result
