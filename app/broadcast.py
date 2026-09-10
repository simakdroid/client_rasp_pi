from __future__ import annotations

import asyncio
from typing import Any


class ClientLimitError(RuntimeError):
    """Raised when the hub already has the maximum number of subscribers."""


class BroadcastHub:
    """Fan-out for small JSON messages; slow clients are told to resync."""

    def __init__(self, queue_size: int = 32, max_clients: int = 32) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        if max_clients < 1:
            raise ValueError("max_clients must be a positive integer")
        self.queue_size = queue_size
        self.max_clients = max_clients
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self.queue_size)
        async with self._lock:
            if len(self._clients) >= self.max_clients:
                raise ClientLimitError("too many websocket clients")
            self._clients.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._clients.discard(queue)

    def client_count(self) -> int:
        return len(self._clients)

    async def publish(self, message: dict[str, Any]) -> None:
        async with self._lock:
            clients = tuple(self._clients)
        for queue in clients:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync"})
