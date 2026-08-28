from __future__ import annotations

import asyncio
from typing import Any


class BroadcastHub:
    """Fan-out for small JSON messages; slow clients are told to resync."""

    def __init__(self, queue_size: int = 32) -> None:
        self.queue_size = queue_size
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self.queue_size)
        async with self._lock:
            self._clients.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._clients.discard(queue)

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
