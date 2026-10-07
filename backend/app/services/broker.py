import asyncio
from collections import defaultdict, deque
import json
import logging
import time
import threading
from typing import List, Any, Optional
import redis.asyncio as async_redis
import redis
from app.config import config

logger = logging.getLogger(__name__)

class BaseBroker:
    """Message bus interface: supports synchronous publish, async subscribe, status snapshots and cache management."""

    def publish_sync(self, channel: str, message: dict):
        """Publish a message to the given channel synchronously (thread-safe)."""
        raise NotImplementedError

    async def subscribe(self, channel: str) -> Any:
        """Subscribe to a channel asynchronously, returning an asyncio.Queue."""
        raise NotImplementedError

    async def unsubscribe(self, channel: str, subscriber: Any):
        """Unsubscribe and release resources."""
        raise NotImplementedError

    async def get_recent_cache(self, channel: str, limit: int = 150) -> List[dict]:
        """Get the N most recent cached messages of a channel."""
        raise NotImplementedError

    def set_status(self, task_id: str, status_payload: dict):
        """Set the task status snapshot (for WebSocket polling)."""
        raise NotImplementedError

    def get_status(self, task_id: str) -> Optional[dict]:
        """Get the latest status snapshot of a task."""
        raise NotImplementedError

    def clear_status(self, task_id: str):
        """Clear the task status snapshot."""

    def clear_cache(self, channel: str):
        """Clear the channel message cache."""

    async def connect(self):
        """Establish the connection (creates a connection pool in Redis mode)."""

    async def disconnect(self):
        """Disconnect and release resources."""

class InMemoryBroker(BaseBroker):
    def __init__(self):
        self._subscribers = defaultdict(list)
        self._cache = defaultdict(lambda: deque(maxlen=1000))
        self._status_snapshots = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def publish_sync(self, channel: str, message: dict):
        with self._lock:
            self._cache[channel].append(message)
            subs = list(self._subscribers.get(channel, []))
        
        if subs and self._loop:
            try:
                if not self._loop.is_running() or self._loop.is_closed():
                    return
            except RuntimeError:
                return

            def safe_put(queue, msg):
                try:
                    queue.put_nowait(msg)
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        queue.put_nowait(msg)
                    except asyncio.QueueFull:
                        pass

            for q in subs:
                try:
                    self._loop.call_soon_threadsafe(safe_put, q, message)
                except (RuntimeError, OSError, ValueError):
                    pass

    async def subscribe(self, channel: str) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        self._subscribers[channel].append(q)
        return q

    async def unsubscribe(self, channel: str, q: asyncio.Queue):
        if channel in self._subscribers and q in self._subscribers[channel]:
            self._subscribers[channel].remove(q)

    async def get_recent_cache(self, channel: str, limit: int = 150) -> List[dict]:
        cache_list = list(self._cache[channel])
        return cache_list[-limit:] if limit > 0 else cache_list

    def set_status(self, task_id: str, status_payload: dict):
        status_payload["_updated_at"] = time.time()
        self._status_snapshots[task_id] = status_payload

    def get_status(self, task_id: str) -> Optional[dict]:
        return self._status_snapshots.get(task_id)

    def clear_status(self, task_id: str):
        self._status_snapshots.pop(task_id, None)

    def clear_cache(self, channel: str):
        with self._lock:
            if channel in self._cache:
                self._cache[channel].clear()


class HybridBroker(BaseBroker):
    """
    Hybrid message bus: prefers Redis and automatically degrades to in-memory dispatch
    when Redis is unavailable.

    Core design:
    - publish_sync: always writes to the in-memory cache and dispatches to local subscribers
      first, then attempts to write to Redis
    - Local dispatch keeps working even if Redis is unavailable
    - _listen_loop: runs only when Redis is available; responsible for cross-process /
      cross-machine message dispatch
    - For single-machine deployments (our scenario), _listen_loop is not required
    """

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._sync_redis: Optional[redis.Redis] = None
        self._async_redis: Optional[async_redis.Redis] = None
        self._health_task: Optional[asyncio.Task] = None
        self._redis_available = False
        self._redis_fail_count = 0
        self._redis_last_fail_time = 0.0
        self._REDIS_RETRY_INTERVAL = 10.0
        self._shutting_down = False

        self._subscribers = defaultdict(list)
        self._cache = defaultdict(lambda: deque(maxlen=1000))
        self._status_snapshots = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()
        self._redis_lock = asyncio.Lock()
        self._status_key = "fireguard:task_status"
        self._cache_prefix = "fireguard:cache:"

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    async def connect(self):
        if self._shutting_down:
            return
        async with self._redis_lock:
            if self._async_redis:
                return
            try:
                self._async_redis = async_redis.from_url(self.redis_url, decode_responses=True)
                await self._async_redis.ping()

                self._sync_redis = redis.from_url(self.redis_url, decode_responses=True)
                self._sync_redis.ping()

                self._health_task = asyncio.create_task(self._health_monitor())
                self._redis_available = True
                self._redis_fail_count = 0
                logger.info(f"[HybridBroker] Redis connected, health monitor started on {self.redis_url}")
            except Exception as e:
                self._redis_available = False
                self._health_task = asyncio.create_task(self._health_monitor())
                logger.warning(f"[HybridBroker] Redis unavailable, using in-memory fallback: {e}")

    async def disconnect(self):
        self._shutting_down = True

        if hasattr(self, '_health_task') and self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        async with self._redis_lock:
            await self._close_redis_connections()

        self._redis_available = False
        logger.info("[HybridBroker] Disconnected.")

    async def _close_redis_connections(self):
        if self._async_redis:
            try:
                await self._async_redis.close()
            except Exception:
                pass
        if self._sync_redis:
            try:
                self._sync_redis.close()
            except Exception:
                pass
        self._async_redis = None
        self._sync_redis = None

    async def _health_monitor(self):
        """Redis health monitoring: periodic ping checks the connection state and automatically flags availability"""
        while not self._shutting_down:
            try:
                await asyncio.sleep(5.0)
                if self._shutting_down:
                    break

                async with self._redis_lock:
                    if self._async_redis:
                        try:
                            await self._async_redis.ping()
                            if not self._redis_available:
                                self._redis_available = True
                                self._redis_fail_count = 0
                                logger.info("[HybridBroker] Redis connection restored")
                        except Exception as e:
                            if self._redis_available:
                                logger.warning(f"[HybridBroker] Redis health check failed: {e}")
                            self._redis_available = False
                            await self._close_redis_connections()
                    else:
                        if self._shutting_down:
                            break
                        try:
                            async_redis_client = async_redis.from_url(self.redis_url, decode_responses=True)
                            await async_redis_client.ping()
                            sync_redis_client = redis.from_url(self.redis_url, decode_responses=True)
                            sync_redis_client.ping()
                            self._async_redis = async_redis_client
                            self._sync_redis = sync_redis_client
                            self._redis_available = True
                            self._redis_fail_count = 0
                            logger.info("[HybridBroker] Redis reconnected via health monitor")
                        except Exception:
                            self._redis_available = False
            except asyncio.CancelledError:
                break
            except Exception:
                if self._shutting_down:
                    break

    def publish_sync(self, channel: str, message: dict):
        """
        Core method: always performs local in-memory dispatch first, then tries Redis persistence.
        Even if Redis is completely unavailable, local WebSocket clients still receive messages.
        """
        with self._lock:
            self._cache[channel].append(message)
            subs = list(self._subscribers.get(channel, []))
        
        if subs and self._loop:
            try:
                if self._loop.is_running() and not self._loop.is_closed():
                    def safe_put(queue, msg):
                        try:
                            queue.put_nowait(msg)
                        except asyncio.QueueFull:
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                            try:
                                queue.put_nowait(msg)
                            except asyncio.QueueFull:
                                pass

                    for q in subs:
                        try:
                            self._loop.call_soon_threadsafe(safe_put, q, message)
                        except (RuntimeError, OSError, ValueError):
                            pass
            except RuntimeError:
                pass

        self._try_redis_publish(channel, message)

    def _try_redis_publish(self, channel: str, message: dict):
        now = time.time()
        if self._redis_fail_count >= 3 and (now - self._redis_last_fail_time) < self._REDIS_RETRY_INTERVAL:
            return

        if not self._sync_redis or not self._redis_available:
            return
        
        payload = json.dumps(message)
        cache_key = f"{self._cache_prefix}{channel}"
        
        try:
            pipeline = self._sync_redis.pipeline()
            pipeline.publish(channel, payload)
            pipeline.lpush(cache_key, payload)
            pipeline.ltrim(cache_key, 0, 499)
            pipeline.execute()
            
            if self._redis_fail_count > 0:
                logger.info(f"[HybridBroker] Redis recovered after {self._redis_fail_count} failures")
            self._redis_fail_count = 0
            self._redis_available = True
        except Exception as e:
            self._redis_fail_count += 1
            self._redis_last_fail_time = now
            self._redis_available = False
            if self._redis_fail_count <= 3 or self._redis_fail_count % 30 == 0:
                logger.warning(
                    f"[HybridBroker] Redis publish failed (count={self._redis_fail_count}), "
                    f"using in-memory fallback: {e}"
                )

    async def subscribe(self, channel: str) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        self._subscribers[channel].append(q)
        return q

    async def unsubscribe(self, channel: str, q: asyncio.Queue):
        if channel in self._subscribers and q in self._subscribers[channel]:
            self._subscribers[channel].remove(q)
            
            if not self._subscribers[channel]:
                del self._subscribers[channel]

    async def get_recent_cache(self, channel: str, limit: int = 150) -> List[dict]:
        cache_list = list(self._cache[channel])
        if cache_list:
            return cache_list[-limit:] if limit > 0 else cache_list
        
        if self._async_redis and self._redis_available:
            try:
                cache_key = f"{self._cache_prefix}{channel}"
                items = await self._async_redis.lrange(cache_key, 0, limit - 1)
                return [json.loads(i) for i in reversed(items)]
            except Exception:
                pass
        
        return []

    def set_status(self, task_id: str, status_payload: dict):
        status_payload["_updated_at"] = time.time()
        self._status_snapshots[task_id] = status_payload
        logger.info(f"[Broker] set_status: key={task_id} type={status_payload.get('type','?')} url={status_payload.get('hls_url','-')[:60]}")

        if self._sync_redis and self._redis_available:
            try:
                self._sync_redis.hset(self._status_key, task_id, json.dumps(status_payload))
            except Exception as e:
                self._redis_available = False
                self._redis_fail_count += 1
                self._redis_last_fail_time = time.time()
                if self._redis_fail_count <= 3:
                    logger.warning(f"[HybridBroker] Redis set_status failed, using in-memory: {e}")

    def get_status(self, task_id: str) -> Optional[dict]:
        local = self._status_snapshots.get(task_id)
        logger.info(f"[Broker] get_status: key={task_id} found={local is not None} type={local.get('type','?') if local else 'None'}")
        if local:
            return local

        if self._sync_redis and self._redis_available:
            try:
                data = self._sync_redis.hget(self._status_key, task_id)
                if data:
                    logger.info(f"[Broker] get_status: found in Redis for {task_id}")
                    return json.loads(data)
            except Exception:
                pass

        logger.warning(f"[Broker] get_status: MISS for {task_id}")
        return None

    def clear_status(self, task_id: str):
        self._status_snapshots.pop(task_id, None)
        if self._sync_redis and self._redis_available:
            try:
                self._sync_redis.hdel(self._status_key, task_id)
            except Exception:
                pass

    def clear_cache(self, channel: str):
        with self._lock:
            if channel in self._cache:
                self._cache[channel].clear()
        if self._sync_redis and self._redis_available:
            try:
                cache_key = f"{self._cache_prefix}{channel}"
                self._sync_redis.delete(cache_key)
            except Exception:
                pass


# ── Factory ───────────────────────────────────────────────────────────────────

if config.REDIS_URL:
    logger.info(f"[Broker] Using HybridBroker (Redis + InMemory fallback) -> {config.REDIS_URL}")
    broker = HybridBroker(config.REDIS_URL)
else:
    logger.info("[Broker] Using InMemoryBroker (Fallback Mode)")
    broker = InMemoryBroker()
