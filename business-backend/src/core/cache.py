import json
from typing import Optional, Any, cast
import redis.asyncio as redis
from src.core.config import settings


class RedisCache:
    def __init__(self) -> None:
        self._client: Optional[redis.Redis] = None

    @property
    def client(self) -> redis.Redis:
        if not self._client:
            raise RuntimeError("Redis not connected")
        return self._client

    async def connect(self) -> None:
        # redis-py's from_url classmethod itself has no return type annotation
        # (upstream gap, not ours) — mypy flags the call as untyped regardless
        # of the assignment target's declared type.
        self._client = redis.from_url(  # type: ignore[no-untyped-call]
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
        )

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get(self, key: str) -> Optional[str]:
        if not self._client:
            raise RuntimeError("Redis not connected")
        # redis-py's get() is typed Union[Awaitable[Any], Any] (shared
        # sync/async stub) — cast documents the real, decode_responses=True
        # return type instead of letting Any leak out of this method.
        return cast(Optional[str], await self._client.get(key))

    async def get_json(self, key: str) -> Optional[Any]:
        data = await self.get(key)
        return json.loads(data) if data else None

    async def set(self, key: str, value: str, ttl: int = 300) -> None:
        if not self._client:
            raise RuntimeError("Redis not connected")
        await self._client.setex(key, ttl, value)

    async def set_json(self, key: str, value: Any, ttl: int = 300) -> None:
        await self.set(key, json.dumps(value), ttl)

    async def delete(self, key: str) -> None:
        if not self._client:
            raise RuntimeError("Redis not connected")
        await self._client.delete(key)

    async def incr(self, key: str) -> int:
        if not self._client:
            raise RuntimeError("Redis not connected")
        return cast(int, await self._client.incr(key))

    async def expire(self, key: str, seconds: int) -> None:
        if not self._client:
            raise RuntimeError("Redis not connected")
        await self._client.expire(key, seconds)


redis_client = RedisCache()
