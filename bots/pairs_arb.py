"""
pairs_arb.py — pairs / stat-arb on correlated spot assets.

Each tick, collects mid-prices for every tradable spot asset. Once we have
>= WARMUP ticks of joint history, finds the top K most-correlated pairs, then
trades the spread z-score:
- z > ENTRY: short the "rich" leg, long the "cheap" leg.
- |z| < EXIT: unwind.
- Hard time stop after MAX_HOLD_TICKS to prevent stale positions.

Uses numpy (allowed library). Dollar-neutral sizing within a small leg cap.
"""

import os
import time
import math
import logging
from collections import deque, defaultdict

import numpy as np

from knight_trader import ExchangeClient

log = logging.getLogger("pairs_arb")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

WARMUP = 60
WINDOW = 60
TOP_K_PAIRS = 4
MIN_CORR = 0.6
ENTRY_Z = 2.0
EXIT_Z = 0.5
STOP_Z = 4.0
MAX_HOLD_TICKS = 120
LEG_CAPITAL_PCT = 0.06
BAILOUT_HALT_EQUITY = 150_000
SPOT_TYPES = {"spot", "equity", "equities"}


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _asset_type(asset):
    return (asset.get("asset_type") or asset.get("type") or "").lower()


def _symbol_of(asset):
    return asset.get("symbol") or asset.get("id") or asset.get("name")


def _best_prices(book):
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bp = [_num(k) for k in bids.keys() if _num(k) > 0]
    ap = [_num(k) for k in asks.keys() if _num(k) > 0]
    return (max(bp) if bp else None, min(ap) if ap else None)


def _team_equity(team_state):
    for key in ("total_equity", "equity", "leaderboard_equity", "net_equity"):
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


class PairsArb:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols = []
        self.histories = defaultdict(lambda: deque(maxlen=WINDOW * 4))
        self.pairs = []  # list of (a, b, hedge_ratio)
        self.positions = {}  # pair_key -> {"side": ..., "a_qty": ..., "b_qty": ..., "opened_tick": ...}
        self.last_pair_refresh = 0.0
        self.last_discovery = 0.0
        self.tick_count = 0

    def discover(self, now):
        if now - self.last_discovery < 30.0 and self.symbols:
            return
        try:
            assets = self.client.get_assets() or []
        except Exception:
            return
        if isinstance(assets, dict):
            assets = list(assets.values())
        syms = []
        for a in assets:
            if _asset_type(a) in SPOT_TYPES and not a.get("halted"):
                sym = _symbol_of(a)
                if sym:
                    syms.append(sym)
        self.symbols = syms[:20]
        self.last_discovery = now

    def update_history(self, state):
        books = state.get("book") or state.get("books") or {}
        for sym in self.symbols:
            book = books.get(sym) or {}
            bid, ask = _best_prices(book)
            if bid and ask:
                self.histories[sym].append(0.5 * (bid + ask))

    def refresh_pairs(self, now):
        if now - self.last_pair_refresh < 30.0 and self.pairs:
            return
        ready = [s for s in self.symbols if len(self.histories[s]) >= WARMUP]
        if len(ready) < 2:
            return
        # Build a matrix of the last WINDOW values for each ready symbol.
        window = min(WINDOW, min(len(self.histories[s]) for s in ready))
        mat = np.array([list(self.histories[s])[-window:] for s in ready])
        # Log-returns for correlation stability.
        rets = np.diff(np.log(mat + 1e-9), axis=1)
        if rets.shape[1] < 5:
            return
        corr = np.corrcoef(rets)
        candidates = []
        for i in range(len(ready)):
            for j in range(i + 1, len(ready)):
                c = corr[i, j]
                if c >= MIN_CORR:
                    # Hedge ratio via OLS on price levels: b_price ~ beta * a_price
                    a_p = mat[i]
                    b_p = mat[j]
                    beta = np.dot(a_p, b_p) / np.dot(a_p, a_p) if np.dot(a_p, a_p) > 0 else 1.0
                    candidates.append((c, ready[i], ready[j], float(beta)))
        candidates.sort(reverse=True)
        self.pairs = [(a, b, beta) for (_, a, b, beta) in candidates[:TOP_K_PAIRS]]
        self.last_pair_refresh = now
        log.info("active pairs: %s", [(a, b) for (a, b, _) in self.pairs])

    def spread_z(self, a, b, beta):
        ha = list(self.histories[a])[-WINDOW:]
        hb = list(self.histories[b])[-WINDOW:]
        n = min(len(ha), len(hb))
        if n < 20:
            return None, None
        ha = np.array(ha[-n:])
        hb = np.array(hb[-n:])
        spread = hb - beta * ha
        mu = spread.mean()
        sd = spread.std() or 1e-9
        return (spread[-1] - mu) / sd, spread[-1]

    def try_trade(self, pair_key, a, b, beta, z, capital, books):
        pos = self.positions.get(pair_key)

        a_bid, a_ask = _best_prices(books.get(a) or {})
        b_bid, b_ask = _best_prices(books.get(b) or {})
        if not all((a_bid, a_ask, b_bid, b_ask)):
            return

        if pos:
            age = self.tick_count - pos["opened_tick"]
            # Hard stop on blowup or time.
            hit_exit = abs(z) < EXIT_Z
            hit_stop = abs(z) > STOP_Z or age > MAX_HOLD_TICKS
            if hit_exit or hit_stop:
                try:
                    if pos["side"] == "long_a":
                        self.client.sell(a, round(a_bid, 4), pos["a_qty"])
                        self.client.buy(b, round(b_ask, 4), pos["b_qty"])
                    else:
                        self.client.buy(a, round(a_ask, 4), pos["a_qty"])
                        self.client.sell(b, round(b_bid, 4), pos["b_qty"])
                    log.info("EXIT pair=%s side=%s z=%.2f age=%d", pair_key, pos["side"], z, age)
                except Exception as exc:
                    log.warning("pair exit failed %s: %s", pair_key, exc)
                self.positions.pop(pair_key, None)
            return

        # Entry: z >= +ENTRY → spread (b - beta*a) is high → short b, long a.
        #        z <= -ENTRY → spread is low → long b, short a.
        if abs(z) < ENTRY_Z:
            return

        leg_notional = LEG_CAPITAL_PCT * capital
        a_mid = 0.5 * (a_bid + a_ask)
        b_mid = 0.5 * (b_bid + b_ask)
        a_qty = round(leg_notional / max(a_mid, 1e-6), 4)
        b_qty = round(leg_notional / max(b_mid, 1e-6), 4)
        if a_qty <= 0 or b_qty <= 0:
            return

        try:
            if z >= ENTRY_Z:
                self.client.buy(a, round(a_ask, 4), a_qty)
                self.client.sell(b, round(b_bid, 4), b_qty)
                side = "long_a"
            else:
                self.client.sell(a, round(a_bid, 4), a_qty)
                self.client.buy(b, round(b_ask, 4), b_qty)
                side = "short_a"
            self.positions[pair_key] = {
                "side": side, "a_qty": a_qty, "b_qty": b_qty, "opened_tick": self.tick_count
            }
            log.info("ENTER pair=%s side=%s z=%.2f qtyA=%.2f qtyB=%.2f", pair_key, side, z, a_qty, b_qty)
        except Exception as exc:
            log.warning("pair entry failed %s: %s", pair_key, exc)

    def run(self):
        last_team_fetch = 0.0
        team_state = {}
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                if state.get("competition_state") != "live":
                    continue
                self.tick_count += 1
                self.discover(now)
                self.update_history(state)
                self.refresh_pairs(now)
                if not self.pairs:
                    continue

                if now - last_team_fetch > 2.0:
                    try:
                        team_state = self.client.get_team_state() or {}
                    except Exception:
                        pass
                    last_team_fetch = now

                equity = _team_equity(team_state)
                if equity and equity < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT GUARD: equity %.0f — halting", equity)
                    try:
                        self.client.cancel_all()
                    except Exception:
                        pass
                    time.sleep(2.0)
                    continue

                capital = _bot_capital(team_state, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}
                for a, b, beta in self.pairs:
                    z, _ = self.spread_z(a, b, beta)
                    if z is None:
                        continue
                    self.try_trade(f"{a}|{b}", a, b, beta, z, capital, books)
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(1.0)


if __name__ == "__main__":
    PairsArb().run()
