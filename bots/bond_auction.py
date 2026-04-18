"""
bond_auction.py — event-driven bond auction bidder.

Bonds auction at $1000 par. The stop-out yield becomes the coupon rate, so the
objective is to win bonds at a yield at or above our fair yield estimate
(derived from the ior_rate timeseries plus a small risk spread).

Because auctions are sporadic and lifecycle events are admin-managed, this bot
mostly idles, polls get_assets() for new bond listings with an open auction
window, and submits one bid per bond. Position cap keeps risk bounded.
"""

import os
import time
import logging

from knight_trader import ExchangeClient

log = logging.getLogger("bond_auction")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

BOND_TYPES = {"bond", "fixed_income"}
YIELD_SPREAD = 0.005           # +50bps over risk-free to demand compensation
MAX_UNITS_PER_AUCTION = 10     # par * units locks capital
MAX_BOND_PCT_OF_CAPITAL = 0.15
POLL_SEC = 10.0
BAILOUT_HALT_EQUITY = 150_000


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _asset_type(asset):
    return (asset.get("asset_type") or asset.get("type") or "").lower()


def _symbol_of(asset):
    return asset.get("symbol") or asset.get("id") or asset.get("name")


def _team_equity(team_state):
    for key in ("total_equity", "equity", "leaderboard_equity"):
        if key in team_state:
            return _num(team_state[key])
    return 0.0


def _bot_capital(team_state, bot_id):
    bots = team_state.get("bots") or team_state.get("portfolio", {}).get("bots") or {}
    if isinstance(bots, dict):
        bot = bots.get(bot_id) or {}
    else:
        bot = next((b for b in bots if b.get("id") == bot_id or b.get("bot_id") == bot_id), {})
    return _num(bot.get("capital") or bot.get("allocated_capital"))


def _latest_ior(client):
    for name in ("ior_rate", "fed_rate"):
        try:
            pts = client.get_timeseries(name, limit=1) or []
            if pts:
                last = pts[-1]
                v = last.get("v") if isinstance(last, dict) else last
                r = _num(v)
                if r > 0:
                    return r
        except Exception:
            continue
    return 0.05  # conservative default


def _auction_is_open(asset):
    # Accept any of several likely flags — schema isn't fully documented.
    for key in ("auction_open", "auction_active", "is_auctioning"):
        if asset.get(key):
            return True
    state = (asset.get("auction_state") or asset.get("lifecycle") or asset.get("state") or "").lower()
    return state in {"auction", "auctioning", "open", "when_issued"}


def run():
    client = ExchangeClient()
    seen = set()
    while True:
        try:
            team_state = {}
            try:
                team_state = client.get_team_state() or {}
            except Exception:
                pass

            equity = _team_equity(team_state)
            if equity and equity < BAILOUT_HALT_EQUITY:
                log.error("BAILOUT GUARD: equity %.0f — skipping auction cycle", equity)
                time.sleep(POLL_SEC)
                continue

            capital = _bot_capital(team_state, BOT_ID) or 100_000.0
            try:
                assets = client.get_assets() or []
            except Exception as exc:
                log.warning("get_assets failed: %s", exc)
                time.sleep(POLL_SEC)
                continue
            if isinstance(assets, dict):
                assets = list(assets.values())

            ior = _latest_ior(client)
            fair_yield = ior + YIELD_SPREAD
            max_bond_notional = MAX_BOND_PCT_OF_CAPITAL * capital
            max_units_cap = int(max_bond_notional // 1000)

            for a in assets:
                if _asset_type(a) not in BOND_TYPES:
                    continue
                sym = _symbol_of(a)
                if not sym or sym in seen:
                    continue
                if not _auction_is_open(a):
                    continue
                units = max(1, min(MAX_UNITS_PER_AUCTION, max_units_cap))
                if units <= 0:
                    continue
                bid_yield = round(fair_yield, 6)
                try:
                    client.place_auction_bid(sym, bid_yield, units)
                    seen.add(sym)
                    log.info("BID %s yield=%.4f units=%d", sym, bid_yield, units)
                except Exception as exc:
                    log.warning("auction bid failed %s: %s", sym, exc)
            time.sleep(POLL_SEC)
        except Exception as exc:
            log.exception("loop error: %s", exc)
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    run()
