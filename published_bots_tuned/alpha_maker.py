"""AUTO-TUNED by backtest/tune.py
bot: alpha_maker
trials: 60
baseline_score: 9546.55
best_score: 27273.68
improvement: +17727.13
best_overrides: {'MIN_SPREAD': 0.05, 'QUOTE_INSIDE': 0.02, 'BASE_SIZE_PCT': 0.02, 'MAX_INVENTORY_PCT': 0.08, 'SKEW_STRENGTH': 0.7, 'SR_WINDOW': 120, 'MR_BAND_PCT': 0.03, 'MR_SIZE_PCT': 0.1, 'MAX_SYMBOLS': 12}

Generated copy — edit the source in published_bots/ and re-run tune.py instead.
"""
"""
alpha_maker.py — workhorse composite bot.

Ensembles three related strategies from the library into one file so a single
dashboard slot carries all three:

  (1) mm_spot           — two-sided quoting on every tradable spot/fx book
  (2) support_resistance — rolling S/R bands serve as a fair-value anchor and
                           pull quotes toward value when the mid is mispriced
  (3) mean_reversion    — when mid crosses outside S/R by >1%, size up on the
                           reverting side (MR overlay on top of the MM book)

Half-Kelly sizing throughout. Inventory-skewed quotes. Self-match guard.
Bailout floor at $150k equity — flatten + idle.

Upload as a single file. Container injects BOT_ID and EXCHANGE_URL.
"""

import logging
import os
import time
from collections import defaultdict, deque

from knight_trader import ExchangeClient

log = logging.getLogger("alpha_maker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

# MM parameters ------------------------------------------------------------
MIN_SPREAD           = 0.05
QUOTE_INSIDE         = 0.02
BASE_SIZE_PCT        = 0.02
MAX_INVENTORY_PCT    = 0.08
SKEW_STRENGTH        = 0.7
MIN_REFRESH_SEC      = 0.20
MAX_SYMBOLS          = 12 

# S/R + MR overlay parameters ---------------------------------------------
SR_WINDOW            = 120      # ticks for support/resistance rolling window
MR_BAND_PCT          = 0.03    # mid must cross S/R by 1% to trigger MR
MR_SIZE_PCT          = 0.1    # extra size when MR overlay fires
MR_MAX_HOLD_SEC      = 600.0   # auto-flatten MR position after 10 min

# Risk ---------------------------------------------------------------------
BAILOUT_HALT_EQUITY  = 25_000

# Outlier guard — reject aggressive crosses more than N% from rolling trade-print mean.
SANE_MAX_DEV        = 0.05     # gate for MR entries
SANE_FLAT_MAX_DEV   = 0.15     # wider band for stale-position flattens

TARGET_TYPES = {"spot", "equity", "equities", "forex", "fx"}


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _asset_type(a): return (a.get("asset_type") or a.get("type") or "").lower()
def _symbol(a):     return a.get("symbol") or a.get("id") or a.get("name")
def _tradable(a):
    if a.get("halted"): return False
    if "tradable" in a and not a["tradable"]: return False
    return True


def _best(book):
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bp = [_num(k) for k in bids.keys() if _num(k) > 0]
    ap = [_num(k) for k in asks.keys() if _num(k) > 0]
    return (max(bp) if bp else None, min(ap) if ap else None)


def _sane_aggressive(px, prints, max_dev=SANE_MAX_DEV):
    """True if crossing at px is sane vs recent trade prints. Allows first trade
    (no history) through; after that, reject prices more than max_dev from
    mean(prints). Protects against outlier one-sided quotes on thin books."""
    if px is None or px <= 0:
        return False
    pxs = list(prints) if prints else []
    if not pxs:
        return True
    ref = sum(pxs) / len(pxs)
    if ref <= 0:
        return True
    return abs(px - ref) / ref <= max_dev


def _team_equity(team):
    for key in ("total_equity", "equity", "leaderboard_equity", "net_equity"):
        if key in team:
            return _num(team[key])
    rud = _num(team.get("rud") or team.get("treasury", {}).get("rud"))
    bots_raw = team.get("bots") or {}
    bots_iter = bots_raw.values() if isinstance(bots_raw, dict) else bots_raw
    cap = sum(_num(b.get("capital") or b.get("allocated_capital")) for b in bots_iter)
    return rud + cap


def _bot(team, bot_id):
    bots = team.get("bots") or team.get("portfolio", {}).get("bots") or {}
    if isinstance(bots, dict):
        return bots.get(bot_id) or {}
    return next((b for b in bots if b.get("id") == bot_id or b.get("bot_id") == bot_id), {})


def _bot_capital(team, bot_id):
    b = _bot(team, bot_id)
    return _num(b.get("capital") or b.get("allocated_capital") or b.get("uncommitted_capital"))


def _bot_positions(team, bot_id):
    raw = _bot(team, bot_id).get("positions") or _bot(team, bot_id).get("inventory") or {}
    out = {}
    if isinstance(raw, dict):
        for s, v in raw.items():
            out[s] = _num(v.get("quantity") or v.get("qty") or v.get("size")) if isinstance(v, dict) else _num(v)
    elif isinstance(raw, list):
        for row in raw:
            s = row.get("symbol") or row.get("asset")
            if s: out[s] = _num(row.get("quantity") or row.get("qty") or row.get("size"))
    return out


class AlphaMaker:
    def __init__(self):
        self.client = ExchangeClient()
        self.resting = defaultdict(lambda: {"bid": None, "ask": None, "bid_px": None, "ask_px": None, "ts": 0.0})
        self.symbols = []
        self.meta = {}
        self.prints = defaultdict(lambda: deque(maxlen=SR_WINDOW))  # symbol -> recent trade prices
        self.mr_open = {}  # symbol -> (side, qty, entry_ts)
        self.last_refresh_symbols = 0.0
        self.mr_skip_logged = {}  # symbol -> last skip-log ts (throttles spam)
        self.halted = False

    def refresh_symbols(self, now):
        if now - self.last_refresh_symbols < 30.0 and self.symbols:
            return
        try:
            assets = self.client.get_assets() or []
        except Exception as exc:
            log.warning("get_assets failed: %s", exc)
            return
        if isinstance(assets, dict): assets = list(assets.values())
        picked = []
        for a in assets:
            if _asset_type(a) not in TARGET_TYPES or not _tradable(a): continue
            sym = _symbol(a)
            if not sym: continue
            picked.append(sym)
            self.meta[sym] = a
            if len(picked) >= MAX_SYMBOLS: break
        self.symbols = picked
        self.last_refresh_symbols = now
        log.info("tracking %d symbols", len(picked))

    def ingest_trades(self, state):
        trades = state.get("recent_trades") or state.get("trades") or []
        for t in trades:
            sym = t.get("symbol")
            px = _num(t.get("price"))
            if sym and px > 0:
                self.prints[sym].append(px)

    def support_resistance(self, symbol):
        """Rolling S/R: avg of 3 lowest and 3 highest recent prints."""
        pxs = list(self.prints[symbol])
        if len(pxs) < 10:
            return None, None
        lows = sorted(pxs)[:3]
        highs = sorted(pxs)[-3:]
        return sum(lows) / len(lows), sum(highs) / len(highs)

    def cancel_side(self, sym, side):
        rec = self.resting[sym]
        oid = rec.get(side)
        if oid:
            try: self.client.cancel(oid)
            except Exception: pass
            rec[side] = None
            rec[f"{side}_px"] = None

    def compute_quotes(self, sym, book, inv, cap):
        bb, ba = _best(book)
        if not bb or not ba or bb >= ba: return None
        spread = ba - bb
        if spread < MIN_SPREAD: return None
        mid = 0.5 * (bb + ba)

        # S/R anchor: pull quotes toward fair value when S/R available
        support, resistance = self.support_resistance(sym)
        anchor = 0.5 * (support + resistance) if support and resistance else mid

        max_inv_notional = MAX_INVENTORY_PCT * max(cap, 1.0)
        inv_notional = inv * mid
        sat = 0.0 if max_inv_notional <= 0 else max(-1.0, min(1.0, inv_notional / max_inv_notional))
        half = spread / 2.0

        # Blend inventory skew with anchor pull: if mid > anchor, bias quotes down
        anchor_skew = 0.5 * half * max(-1.0, min(1.0, (mid - anchor) / max(anchor, 1e-9) / 0.02))
        inv_skew = SKEW_STRENGTH * half * sat
        skew = inv_skew + anchor_skew

        bid_px = round(min(bb + QUOTE_INSIDE, mid - QUOTE_INSIDE) - skew, 4)
        ask_px = round(max(ba - QUOTE_INSIDE, mid + QUOTE_INSIDE) - skew, 4)
        if bid_px >= ask_px: return None

        bid_scale = max(0.0, 1.0 - max(0.0, sat))
        ask_scale = max(0.0, 1.0 + min(0.0, sat))
        raw = (BASE_SIZE_PCT * cap) / mid if mid > 0 else 0.0
        return bid_px, ask_px, round(raw * bid_scale, 4), round(raw * ask_scale, 4), mid, support, resistance

    def mr_overlay(self, sym, bb, ba, support, resistance, cap):
        """Mean-reversion overlay: aggressive size when mid crosses S/R by >1%."""
        if not support or not resistance: return
        mid = 0.5 * (bb + ba)
        now = time.monotonic()
        # Flatten stale MR position
        if sym in self.mr_open:
            side, qty, ts = self.mr_open[sym]
            if now - ts > MR_MAX_HOLD_SEC:
                flat_px = bb if side == "long" else ba
                if _sane_aggressive(flat_px, self.prints[sym], SANE_FLAT_MAX_DEV):
                    try:
                        (self.client.sell if side == "long" else self.client.buy)(sym, flat_px, qty)
                        log.info("MR auto-flat %s %s %s", sym, side, qty)
                    except Exception: pass
                    self.mr_open.pop(sym, None)
                else:
                    log.warning("MR auto-flat %s deferred — %s=%s outside ±%.0f%% of recent prints",
                                sym, "bid" if side == "long" else "ask", flat_px, SANE_FLAT_MAX_DEV * 100)

        if sym in self.mr_open: return  # already holding
        size = round((MR_SIZE_PCT * cap) / mid, 4)
        if size <= 0: return
        if mid < support * (1.0 - MR_BAND_PCT):
            if not _sane_aggressive(ba, self.prints[sym]):
                if now - self.mr_skip_logged.get(sym, 0.0) > 30.0:
                    log.warning("MR long %s skipped — ask %s outside ±%.0f%% band vs prints", sym, ba, SANE_MAX_DEV * 100)
                    self.mr_skip_logged[sym] = now
                return
            try:
                self.client.buy(sym, ba, size)
                self.mr_open[sym] = ("long", size, now)
                log.info("MR LONG %s qty=%s @ %s (support %s)", sym, size, ba, support)
            except Exception as exc:
                log.warning("MR long %s failed: %s", sym, exc)
        elif mid > resistance * (1.0 + MR_BAND_PCT):
            if not _sane_aggressive(bb, self.prints[sym]):
                if now - self.mr_skip_logged.get(sym, 0.0) > 30.0:
                    log.warning("MR short %s skipped — bid %s outside ±%.0f%% band vs prints", sym, bb, SANE_MAX_DEV * 100)
                    self.mr_skip_logged[sym] = now
                return
            try:
                self.client.sell(sym, bb, size)
                self.mr_open[sym] = ("short", size, now)
                log.info("MR SHORT %s qty=%s @ %s (resistance %s)", sym, size, bb, resistance)
            except Exception as exc:
                log.warning("MR short %s failed: %s", sym, exc)

    def quote_symbol(self, sym, book, team, cap, now):
        inv = _bot_positions(team, BOT_ID).get(sym, 0.0)
        target = self.compute_quotes(sym, book, inv, cap)
        rec = self.resting[sym]
        if not target:
            if rec["bid"] or rec["ask"]:
                self.cancel_side(sym, "bid"); self.cancel_side(sym, "ask")
            return
        bid_px, ask_px, bid_q, ask_q, mid, support, resistance = target
        if now - rec["ts"] < MIN_REFRESH_SEC and rec["bid_px"] == bid_px and rec["ask_px"] == ask_px:
            return
        self.cancel_side(sym, "bid"); self.cancel_side(sym, "ask")
        if bid_q > 0:
            try:
                oid = self.client.buy(sym, bid_px, bid_q)
                if oid: rec["bid"], rec["bid_px"] = oid, bid_px
            except Exception as exc: log.warning("buy %s failed: %s", sym, exc)
        if ask_q > 0:
            try:
                oid = self.client.sell(sym, ask_px, ask_q)
                if oid: rec["ask"], rec["ask_px"] = oid, ask_px
            except Exception as exc: log.warning("sell %s failed: %s", sym, exc)
        rec["ts"] = now
        bb, ba = _best(book)
        self.mr_overlay(sym, bb, ba, support, resistance, cap)

    def flatten_all(self):
        try: self.client.cancel_all()
        except Exception: pass
        self.resting.clear()
        self.mr_open.clear()

    def run(self):
        last_team_fetch = 0.0
        team_state = {}
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                if state.get("competition_state") != "live":
                    if not self.halted:
                        self.flatten_all(); self.halted = True
                    continue
                self.halted = False
                self.refresh_symbols(now)
                self.ingest_trades(state)
                if not self.symbols: continue

                if now - last_team_fetch > 1.0:
                    try: team_state = self.client.get_team_state() or {}
                    except Exception as exc: log.warning("get_team_state failed: %s", exc)
                    last_team_fetch = now

                equity = _team_equity(team_state)
                if equity and equity < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT GUARD: equity %.0f — flattening", equity)
                    self.flatten_all(); time.sleep(1.0); continue

                cap = _bot_capital(team_state, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}
                for sym in self.symbols:
                    book = books.get(sym) or {}
                    if not book:
                        try: book = self.client.get_book(sym) or {}
                        except Exception: book = {}
                    if not book: continue
                    self.quote_symbol(sym, book, team_state, cap, now)
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(0.5)


if __name__ == "__main__":
    AlphaMaker().run()
