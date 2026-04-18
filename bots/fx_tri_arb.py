"""
fx_tri_arb.py — triangular arbitrage across FX pairs.

For each (A, B, C) where A/B, B/C, and A/C are all listed, the no-arb relation
is:

    market(A/C)  ==  market(A/B) * market(B/C)

If the implied cross diverges from the quoted cross by more than the sum of
round-trip half-spreads, fire all three legs in quick succession. We post at
top-of-book to maximize fill probability (this is effectively IOC in practice
given price-time priority).
"""

import os
import time
import logging
import itertools
from collections import defaultdict

from knight_trader import ExchangeClient

log = logging.getLogger("fx_tri_arb")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

FX_TYPES = {"forex", "fx"}
MIN_EDGE_BPS = 15              # min edge after fees/spreads to fire
NOTIONAL_PCT = 0.04            # per-triangle notional as fraction of capital
COOLDOWN_SEC = 0.75            # per-triangle cooldown after firing
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


def _best_prices(book):
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bp = [_num(k) for k in bids.keys() if _num(k) > 0]
    ap = [_num(k) for k in asks.keys() if _num(k) > 0]
    return (max(bp) if bp else None, min(ap) if ap else None)


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


def _parse_pair(symbol):
    """Return (base, quote) if this looks like an FX pair (e.g. 'EUR/USD' or 'EURUSD')."""
    if "/" in symbol:
        a, b = symbol.split("/", 1)
        if len(a) >= 2 and len(b) >= 2:
            return a.upper(), b.upper()
    if "-" in symbol:
        a, b = symbol.split("-", 1)
        if len(a) >= 2 and len(b) >= 2:
            return a.upper(), b.upper()
    if len(symbol) == 6 and symbol.isalpha():
        return symbol[:3].upper(), symbol[3:].upper()
    return None


class TriArb:
    def __init__(self):
        self.client = ExchangeClient()
        self.pairs = {}          # (base, quote) -> symbol
        self.rev_pairs = {}      # symbol -> (base, quote)
        self.triangles = []      # list of (A, B, C)
        self.last_discovery = 0.0
        self.cooldown = defaultdict(float)

    def discover(self, now):
        if now - self.last_discovery < 30.0 and self.pairs:
            return
        try:
            assets = self.client.get_assets() or []
        except Exception:
            return
        if isinstance(assets, dict):
            assets = list(assets.values())
        pairs = {}
        rev = {}
        for a in assets:
            if _asset_type(a) not in FX_TYPES or a.get("halted"):
                continue
            sym = _symbol_of(a)
            parsed = _parse_pair(sym) if sym else None
            if parsed:
                pairs[parsed] = sym
                rev[sym] = parsed
        self.pairs = pairs
        self.rev_pairs = rev
        currencies = {c for pair in pairs.keys() for c in pair}
        triangles = []
        for A, B, C in itertools.permutations(currencies, 3):
            if (A, B) in pairs and (B, C) in pairs and (A, C) in pairs:
                if (A, B, C) not in triangles and (B, A, C) not in triangles:
                    triangles.append((A, B, C))
        self.triangles = triangles
        self.last_discovery = now
        log.info("FX pairs=%d triangles=%d", len(pairs), len(triangles))

    def try_triangle(self, triangle, books, capital, now):
        A, B, C = triangle
        if now < self.cooldown[triangle]:
            return
        ab_sym = self.pairs.get((A, B))
        bc_sym = self.pairs.get((B, C))
        ac_sym = self.pairs.get((A, C))
        if not (ab_sym and bc_sym and ac_sym):
            return
        ab = books.get(ab_sym) or {}
        bc = books.get(bc_sym) or {}
        ac = books.get(ac_sym) or {}
        ab_bid, ab_ask = _best_prices(ab)
        bc_bid, bc_ask = _best_prices(bc)
        ac_bid, ac_ask = _best_prices(ac)
        if not all((ab_bid, ab_ask, bc_bid, bc_ask, ac_bid, ac_ask)):
            return

        # Direction 1: buy A/C at ac_ask vs. synthetic sell via A/B × B/C at ab_bid * bc_bid
        implied_sell = ab_bid * bc_bid
        if implied_sell > ac_ask:
            edge_bps = 1e4 * (implied_sell - ac_ask) / ac_ask
            if edge_bps >= MIN_EDGE_BPS:
                notional = NOTIONAL_PCT * capital
                qty = round(notional / ac_ask, 4)
                if qty > 0:
                    self.fire(triangle, "BUY_AC", ac_sym, ac_ask, qty,
                              ab_sym, ab_bid, qty, bc_sym, bc_bid, qty, edge_bps)
                    self.cooldown[triangle] = now + COOLDOWN_SEC
                    return

        # Direction 2: sell A/C at ac_bid vs. synthetic buy via A/B × B/C at ab_ask * bc_ask
        implied_buy = ab_ask * bc_ask
        if implied_buy < ac_bid:
            edge_bps = 1e4 * (ac_bid - implied_buy) / ac_bid
            if edge_bps >= MIN_EDGE_BPS:
                notional = NOTIONAL_PCT * capital
                qty = round(notional / ac_bid, 4)
                if qty > 0:
                    self.fire(triangle, "SELL_AC", ac_sym, ac_bid, qty,
                              ab_sym, ab_ask, qty, bc_sym, bc_ask, qty, edge_bps)
                    self.cooldown[triangle] = now + COOLDOWN_SEC

    def fire(self, triangle, direction, ac_sym, ac_px, ac_qty,
             ab_sym, ab_px, ab_qty, bc_sym, bc_px, bc_qty, edge_bps):
        log.info("TRIANGLE %s %s edge=%.1fbps", triangle, direction, edge_bps)
        try:
            if direction == "BUY_AC":
                self.client.buy(ac_sym, round(ac_px, 6), ac_qty)
                self.client.sell(ab_sym, round(ab_px, 6), ab_qty)
                self.client.sell(bc_sym, round(bc_px, 6), bc_qty)
            else:
                self.client.sell(ac_sym, round(ac_px, 6), ac_qty)
                self.client.buy(ab_sym, round(ab_px, 6), ab_qty)
                self.client.buy(bc_sym, round(bc_px, 6), bc_qty)
        except Exception as exc:
            log.warning("triangle fire failed %s: %s — attempting to flatten", triangle, exc)
            try:
                self.client.cancel_all()
            except Exception:
                pass

    def run(self):
        last_team_fetch = 0.0
        team_state = {}
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                if state.get("competition_state") != "live":
                    continue
                self.discover(now)
                if not self.triangles:
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
                for tri in self.triangles:
                    self.try_triangle(tri, books, capital, now)
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(1.0)


if __name__ == "__main__":
    TriArb().run()
