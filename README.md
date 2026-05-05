# Kalshi Weather MM

Localhost market-making system for Kalshi weather (KXHIGH / KXLOW) markets across 8 high-liquidity stations. Three jobs:

1. **Theo engine.** Live, well-calibrated bracket-level fair values. EMOS regression on a multi-model Open-Meteo ensemble (ECMWF IFS, GFS, ICON, GEM, JMA), per-station sigma shrinkage, lag-1 residual persistence correction, and a Monte-Carlo running-max sampler for intraday updates.
2. **Quoter.** Post-only top-of-book quotes targeted at Kalshi's top-300-per-side maker incentive tier. Decision tree over (theo − mid) edge with fee-aware fractional Kelly sizing. Cancel-then-replace on theo move, with a 250 ms debounce to avoid cancel storms.
3. **Dashboard.** Single-page htmx + SSE app at `http://localhost:8000` showing theos, mid, our quotes, top-of-book status, position, P&L, and reward-capture %.

A vol controller pulls quotes ahead of scheduled NWP / METAR releases and re-quotes once theos absorb the new info.

## Quick start

```bash
make install     # creates .venv, installs deps
make paper       # boots in paper mode (no live orders)
```

Then open http://localhost:8000.

For live trading:

1. Provision a **trading-tier** Kalshi RSA key. Read-only keys cannot place orders.
2. Save the PEM to `~/.kweather/kalshi.pem` and set `KALSHI_KEY_ID` in `.env` (copy from `.env.example`).
3. `make run` (or `python -m kweather.main --live`). The bot refuses to start if `GET /portfolio/balance` returns 401.

A hard kill switch is available via `KWEATHER_KILL_SWITCH=1` env or the dashboard "Withdraw All" button.

## Layout

```
kweather/
  main.py                     entry point + orchestrator
  config.py types.py auth.py
  market_data/
    kalshi_rest.py kalshi_ws.py orderbook.py
  theo/
    emos.py bracket.py persistence.py sigma_shrink.py engine.py
  weather/
    open_meteo.py cache.py
  quoter/
    decision.py sizer.py placer.py
  vol/
    schedule.py monitor.py controller.py
  rewards/
    tracker.py attribution.py
  web/
    app.py events.py templates/ static/
  storage/
    db.py schema.sql
config/
  stations.yaml risk.yaml schedule.yaml
tests/
scripts/
```

## Theo specifics

- Refit nightly: EMOS coefficients (a, b_i, c, d) per (station, target=high|low) by minimizing closed-form Gaussian CRPS over the last 365 days (expanding window).
- Sigma shrinkage default: 0.90 for Tmax, 1.00 for Tmin.
- Persistence: yesterday's residual r₁ = obs − μ_yest is added back to today's μ with weight ρ (defaults: 0.30 high, 0.20 low).
- Bracket integration uses the +/- 0.5 °F CLI rounding adjustment so a "73-77" bracket becomes the continuous interval [72.5, 77.5).
- Latency budgets (asserted in `theo/engine.py`): "new forecast → theo emit" < 500 ms; "intraday persistence update → theo emit" < 100 ms.
- Intraday Monte Carlo: 5000 paths, AR(1) ρ = 0.85 per minute, σ scaled by HRRR residual std at the relevant lead.

## Quoter decision tree

For each (market, side) at every theo or book tick, with `tight = 2¢` and `stale = 8¢`:

| condition | action |
|---|---|
| `|theo − mid| < tight` | two-sided at theo ± half_spread |
| `theo > mid + tight` | bid only at theo − half_spread |
| `theo < mid − tight` | ask only at theo + half_spread |
| `|theo − mid| > stale` | flat (log for review) |

`half_spread = max(1, 1 + 0.5 σ_f)` cents, scaled by vol-window `widen_factor`. Sizing uses fractional Kelly at 25 % of full Kelly.

## Vol controller

Pulls quotes early and re-quotes after each scheduled vol window. Window sources:

- HRRR (top-of-hour), NBM hourly, GFS / ECMWF / NWS local cycles
- ASOS METAR cycle (every :53), 6-hour METAR cap (00/06/12/18 Z)
- CLI publication (settlement morning)

Probabilistic rules widen rather than cancel: peak-hour ramp (1.25×), high-ensemble-spread day (2×), sea-breeze stations 10–12 local (1.5×). Frontal-passage events trigger a hard cancel from T-15 min through T+60 min.

End-to-end target: ≤ 60 s post-event to re-quote.

## Rewards tracker

Polls `GET /markets/{ticker}/orderbook?depth=300` every minute. For each side, computes whether our resting orders sit within the first 300 contracts at the best price. Records per-minute eligibility and aggregates to a daily capture %.

## Tests

```bash
make test
```

- `test_emos.py` — EMOS fit recovers synthetic coefficients within tolerance.
- `test_bracket.py` — partition sums to 1, centered bracket dominates wings, single-temp rounding.
- `test_decision.py` — decision tree across (theo, mid) tuples.
- `test_orderbook_replay.py` — snapshot + delta replay.
- `test_vol_controller.py` — virtual-clock cancel-at-T-Δ / requote-at-T+60s.
- `test_persistence.py` — lag-1 + running-max sampler.
- `test_storage.py` — SQLite schema + reward aggregation.
- `test_kalshi_parsing.py` — KXHIGH/KXLOW ticker parsing.

## Safety

- Default mode is `paper`. The bot refuses to place live orders unless `--live` is passed.
- Per-market notional cap and per-bracket position cap.
- Daily P&L stop loss (`KWEATHER_DAILY_LOSS_STOP_USD`, default $200).
- Correlation buckets (NE corridor, midwest, etc.) cap aggregate regional exposure.
- Every order has a `client_order_id`; full audit log lives in SQLite (`audit_log` table).
- Hard kill switch (`KWEATHER_KILL_SWITCH=1`) cancels all open orders and refuses placement.

## Acceptance criteria

- `make run` boots and the dashboard at http://localhost:8000 shows ≥ 4 active markets with theos and book data within 30 s.
- `--paper` mode for 1 h: ≥ 50 quotes placed/cancelled across 8+ markets without errors.
- Theos within 10 % on Brier and 0.5 ¢ on point-fair-value vs the offline backtest across ≥ 100 contract-days.
- Vol-controller cancel/requote within 1 s of schedule for 5/5 events in a 24 h replay.
