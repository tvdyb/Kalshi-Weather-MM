"""Kalshi WebSocket client. Subscribes orderbook_delta, trade, and fill channels.

Reconnects with exponential backoff. On reconnect, the caller is expected to resync
each subscribed market via REST GET /markets/{ticker}/orderbook before the WS deltas
are applied.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from kweather.auth import KalshiSigner
from kweather.config import Settings

log = logging.getLogger(__name__)

OnMessageFn = Callable[[dict[str, Any]], Awaitable[None]]


class KalshiWS:
    def __init__(self, settings: Settings, on_message: OnMessageFn):
        self.settings = settings
        self.on_message = on_message
        self.signer: KalshiSigner | None = None
        if settings.kalshi_key_id and settings.kalshi_private_key_path.exists():
            self.signer = KalshiSigner(settings.kalshi_key_id, settings.kalshi_private_key_path)
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._stop = asyncio.Event()
        self._tickers: set[str] = set()
        self._cmd_id = 0
        self._on_reconnect: list[Callable[[], Awaitable[None]]] = []

    def add_reconnect_hook(self, fn: Callable[[], Awaitable[None]]) -> None:
        self._on_reconnect.append(fn)

    def _next_cmd_id(self) -> int:
        self._cmd_id += 1
        return self._cmd_id

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def subscribe(self, tickers: list[str]) -> None:
        new = [t for t in tickers if t not in self._tickers]
        for t in new:
            self._tickers.add(t)
        if self._ws is None or not new:
            return
        for ch in ("orderbook_delta", "trade", "fill"):
            await self._send(
                {
                    "id": self._next_cmd_id(),
                    "cmd": "subscribe",
                    "params": {"channels": [ch], "market_tickers": new},
                }
            )

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(payload))

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_and_loop()
                backoff = 1.0
            except Exception as e:
                log.warning("WS loop error: %s", e)
                jitter = random.uniform(0.5, 1.5)
                await asyncio.sleep(min(30.0, backoff) * jitter)
                backoff = min(backoff * 2.0, 30.0)

    async def _connect_and_loop(self) -> None:
        headers: dict[str, str] = {}
        if self.signer is not None:
            from urllib.parse import urlparse

            path = urlparse(self.settings.kalshi_ws_url).path
            headers = self.signer.headers("GET", path)

        async with websockets.connect(
            self.settings.kalshi_ws_url,
            extra_headers=list(headers.items()) if headers else None,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            log.info("WS connected: %s", self.settings.kalshi_ws_url)
            for fn in self._on_reconnect:
                try:
                    await fn()
                except Exception:
                    log.exception("WS reconnect hook failed")
            if self._tickers:
                for ch in ("orderbook_delta", "trade", "fill"):
                    await self._send(
                        {
                            "id": self._next_cmd_id(),
                            "cmd": "subscribe",
                            "params": {
                                "channels": [ch],
                                "market_tickers": list(self._tickers),
                            },
                        }
                    )

            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                except TimeoutError:
                    continue
                except ConnectionClosed:
                    log.info("WS closed; reconnecting")
                    return
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                try:
                    await self.on_message(msg)
                except Exception:
                    log.exception("WS message handler failed: %r", msg)
