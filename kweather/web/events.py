"""SSE event channel. The TheoEngine, OrderBook updates, and rewards push here.

Each channel is a per-subscriber asyncio.Queue. The publish() call fans out non-blocking;
a slow consumer can drop messages.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, max_queue: int = 1024):
        self._subs: list[asyncio.Queue[str]] = []
        self.max_queue = max_queue

    def publish(self, event: str, data: Any) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        dead: list[asyncio.Queue[str]] = []
        for q in self._subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subs.remove(q)

    async def subscribe(self) -> AsyncIterator[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=self.max_queue)
        self._subs.append(q)
        try:
            while True:
                yield await q.get()
        finally:
            if q in self._subs:
                self._subs.remove(q)
