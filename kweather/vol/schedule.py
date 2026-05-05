"""Vol windows: deterministic schedule + probabilistic detectors.

Generates VolWindow instances looking forward up to `lookahead_hours`. The window
times include cancel_lead_seconds and requote_delay_seconds metadata so the
controller can pull quotes early and re-quote after the next theo refresh.

Probabilistic windows (peak hour, sea-breeze, frontal passage, high ensemble spread)
are surfaced separately as `widen_factor` adjustments rather than cancellations,
except for frontal passages, which produce hard cancel windows.
"""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from kweather.config import Settings, Station
from kweather.types import VolWindow

ET = ZoneInfo("America/New_York")
UTC = UTC


def _next_at(now: datetime, hh: int, mm: int, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def build_windows(
    settings: Settings, now: datetime | None = None, lookahead_hours: int = 12
) -> list[VolWindow]:
    now = now or datetime.now(tz=UTC)
    horizon = now + timedelta(hours=lookahead_hours)
    out: list[VolWindow] = []
    sched = settings.schedule.get("deterministic", [])
    for s in settings.stations:
        for entry in sched:
            out.extend(_windows_from_entry(entry, s, now, horizon))
    out.sort(key=lambda w: w.start_ts)
    return out


def _windows_from_entry(
    entry: dict[str, Any], station: Station, now: datetime, horizon: datetime
) -> list[VolWindow]:
    out: list[VolWindow] = []
    name = entry.get("name", "vol_event")
    cancel_lead = int(entry.get("cancel_lead_seconds", 30))
    requote_delay = int(entry.get("requote_delay_seconds", 60))
    duration = int(entry.get("duration_seconds", 300))
    severity = entry.get("severity", "normal")
    if "cron_minutes" in entry:
        for minute in entry["cron_minutes"]:
            t = now.replace(minute=int(minute), second=0, microsecond=0)
            if t < now:
                t = t + timedelta(hours=1)
            while t <= horizon:
                out.append(
                    VolWindow(
                        name=name,
                        station_code=station.code,
                        start_ts=t,
                        end_ts=t + timedelta(seconds=duration),
                        cancel_lead_seconds=cancel_lead,
                        requote_delay_seconds=requote_delay,
                        severity=severity,
                    )
                )
                t = t + timedelta(hours=1)
    elif "et_local_times" in entry:
        for hhmm in entry["et_local_times"]:
            hh, mm = map(int, hhmm.split(":"))
            tz = ZoneInfo(station.timezone)
            t = _next_at(now, hh, mm, tz)
            while t <= horizon:
                out.append(
                    VolWindow(
                        name=name,
                        station_code=station.code,
                        start_ts=t,
                        end_ts=t + timedelta(seconds=duration),
                        cancel_lead_seconds=cancel_lead,
                        requote_delay_seconds=requote_delay,
                        severity=severity,
                    )
                )
                t = t + timedelta(days=1)
    elif "utc_hours" in entry:
        for hh in entry["utc_hours"]:
            t = now.replace(hour=int(hh), minute=0, second=0, microsecond=0)
            if t < now:
                t = t + timedelta(days=1)
            while t <= horizon:
                out.append(
                    VolWindow(
                        name=name,
                        station_code=station.code,
                        start_ts=t,
                        end_ts=t + timedelta(seconds=duration),
                        cancel_lead_seconds=cancel_lead,
                        requote_delay_seconds=requote_delay,
                        severity=severity,
                    )
                )
                t = t + timedelta(days=1)
    return out


def widen_factors_for(
    settings: Settings, station: Station, now: datetime | None = None
) -> tuple[float, float]:
    """Return (widen_factor, size_factor) implied by probabilistic rules."""
    now = now or datetime.now(tz=UTC)
    tz = ZoneInfo(station.timezone)
    local = now.astimezone(tz)
    widen = 1.0
    size = 1.0
    prob = settings.schedule.get("probabilistic", [])
    for entry in prob:
        rule = entry.get("rule")
        if rule == "peak_hour":
            window = entry.get("local_window", ["13:00", "17:00"])
            if _within_local_window(local, window):
                widen = max(widen, float(entry.get("widen_factor", 1.0)))
                size = min(size, float(entry.get("size_factor", 1.0)))
        elif rule == "sea_breeze_station" and station.code in entry.get("applies_to_stations", []):
            window = entry.get("local_window", ["10:00", "12:00"])
            if _within_local_window(local, window):
                widen = max(widen, float(entry.get("widen_factor", 1.0)))
                size = min(size, float(entry.get("size_factor", 1.0)))
    return widen, size


def _within_local_window(now_local: datetime, window: list[str]) -> bool:
    h1, m1 = map(int, window[0].split(":"))
    h2, m2 = map(int, window[1].split(":"))
    t = now_local.time()
    return time(h1, m1) <= t <= time(h2, m2)
