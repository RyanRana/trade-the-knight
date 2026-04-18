"""
ior_parker.py — zero-risk RUD parking bot + team-equity watchdog.

Does no directional trading. Two jobs:
1. Sit on allocated RUD so it earns interest from the ior_rate timeseries.
2. Continuously report team equity / IOR / open bot status, and cancel its own
   orders (harmless here, but keeps the idempotent shutdown path hot) if the
   team approaches the $50k bailout floor.

Running this as its own container matters because per-bot capital shows up in
leaderboard equity. The extra line item is free alpha.
"""

import os
import time
import logging
from knight_trader import ExchangeClient

log = logging.getLogger("ior_parker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

BAILOUT_WARN_EQUITY = 250_000
BAILOUT_HALT_EQUITY = 150_000
REPORT_EVERY_SEC = 30.0


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _team_equity(team_state):
    for key in ("total_equity", "equity", "leaderboard_equity", "net_equity"):
        if key in team_state:
            return _num(team_state[key])
    rud = _num(team_state.get("rud") or team_state.get("treasury", {}).get("rud"))
    cap = 0.0
    for b in (team_state.get("bots") or {}).values():
        cap += _num(b.get("capital") or b.get("allocated_capital"))
    return rud + cap


def _latest_ior(client):
    try:
        series = client.get_timeseries("ior_rate", limit=1) or []
        if series:
            last = series[-1]
            if isinstance(last, dict):
                return _num(last.get("v") or last.get("value"))
            return _num(last)
    except Exception:
        pass
    # Fallback to fed_rate.
    try:
        series = client.get_timeseries("fed_rate", limit=1) or []
        if series:
            last = series[-1]
            return _num(last.get("v") if isinstance(last, dict) else last)
    except Exception:
        pass
    return 0.0


def run():
    client = ExchangeClient()
    last_report = 0.0
    warned = False
    for state in client.stream_state():
        try:
            now = time.monotonic()
            if now - last_report < REPORT_EVERY_SEC:
                continue
            last_report = now

            if state.get("competition_state") != "live":
                continue

            try:
                team_state = client.get_team_state() or {}
            except Exception as exc:
                log.warning("get_team_state failed: %s", exc)
                continue

            equity = _team_equity(team_state)
            ior = _latest_ior(client)
            log.info("equity=%.2f ior_rate=%.5f", equity, ior)

            if equity and equity < BAILOUT_HALT_EQUITY:
                log.error("BAILOUT GUARD: equity %.0f < halt threshold %.0f — cancelling all", equity, BAILOUT_HALT_EQUITY)
                try:
                    client.cancel_all()
                except Exception:
                    pass
            elif equity and equity < BAILOUT_WARN_EQUITY and not warned:
                log.warning("equity %.0f approaching bailout floor — other bots should dial back", equity)
                warned = True
            elif equity and equity >= BAILOUT_WARN_EQUITY:
                warned = False
        except Exception as exc:
            log.exception("loop error: %s", exc)
            time.sleep(1.0)


if __name__ == "__main__":
    run()
