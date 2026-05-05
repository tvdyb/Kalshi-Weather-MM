"""Vol controller scheduling test using a virtual clock."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kweather.config import load_settings
from kweather.types import VolWindow
from kweather.vol.controller import VolController


@pytest.mark.asyncio
async def test_cancel_lead_and_requote_delay():
    settings = load_settings()
    cancel_calls: list[str] = []
    requote_calls: list[str] = []

    async def cancel(station: str) -> None:
        cancel_calls.append(station)

    async def requote(station: str) -> None:
        requote_calls.append(station)

    async def wait_refresh(ts):
        return None

    fake_now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)

    def clock():
        return fake_now

    controller = VolController(
        settings,
        cancel_station_fn=cancel,
        requote_station_fn=requote,
        wait_for_refresh_fn=wait_refresh,
        clock=clock,
    )
    # Single deterministic window starting at 12:01, lasting 60s, with 30s lead and 60s requote.
    w = VolWindow(
        name="UNIT",
        station_code="KNYC",
        start_ts=fake_now + timedelta(seconds=60),
        end_ts=fake_now + timedelta(seconds=120),
        cancel_lead_seconds=30,
        requote_delay_seconds=60,
    )
    controller.windows = [w]

    # T - 31s: not yet cancel time
    fake_now = w.start_ts - timedelta(seconds=31)
    await controller.step()
    assert cancel_calls == []

    # T - 30s: cancel fires
    fake_now = w.start_ts - timedelta(seconds=30)
    await controller.step()
    assert cancel_calls == ["KNYC"]
    assert w.cancelled is True

    # End + 59s: not yet requote time
    fake_now = w.end_ts + timedelta(seconds=59)
    await controller.step()
    assert requote_calls == []

    # End + 60s: requote fires
    fake_now = w.end_ts + timedelta(seconds=60)
    await controller.step()
    assert requote_calls == ["KNYC"]
    assert w.requoted is True
