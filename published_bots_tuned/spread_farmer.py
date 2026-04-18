"""AUTO-TUNED by backtest/tune.py
bot: spread_farmer
trials: 60
baseline_score: 18191.58
best_score: 54287.17
improvement: +36095.59
best_overrides: {'MIN_SPREAD_BPS': 40, 'MIN_INSIDE': 0.03, 'EDGE_PCT': 0.35, 'BASE_SIZE_PCT': 0.012, 'MAX_INVENTORY_PCT': 0.03, 'SKEW_STRENGTH': 0.5, 'CV_MAX': 0.12, 'MIN_PRINTS': 50, 'MR_ENTRY_Z': 1.5, 'MR_SIZE_PCT': 0.06}

Generated copy — edit the source in published_bots/ and re-run tune.py instead.
"""
"""
spread_farmer.py — selective passive market-maker + HILL mean-reverter.

Built from observed market data (not generic config):
  - QFC, PASS, YARD show cv<0.05 and 80-220 bps spreads → prime MM targets.
  - HILL carries the most print volume with moderate cv (~0.13) → MR target.
  - SCA is blown up (cv>0.4); auto-filter excludes it dynamically.

Layer (1): selective MM
  - Auto-pick symbols each refresh whose rolling coefficient-of-variation (cv)
    is below CV_MAX and quoted spread above MIN_SPREAD_BPS.
  - Quote inside NBBO by max(MIN_INSIDE, EDGE_PCT * spread).
  - Inventory-skewed quotes (half-Kelly sizing).
  - Median-based outlier guard — rejects crosses >3% from rolling median.

Layer (2): HILL mean-reversion overlay
  - 30-tick EMA on HILL prints.
  - Enter long/short when mid z-score > MR_ENTRY_Z.
  - Auto-flatten after MR_MAX_HOLD_SEC or when z reverts inside MR_EXIT_Z.

Orthogonal to cross_section_engine (that bot aggressively crosses; this one
quotes passively). Can run in parallel without position conflict because
exchange tracks inventory per bot_id.

Upload as single file. Container injects BOT_ID + EXCHANGE_URL.
"""

import logging
import math
import os
import time
from collections import defaultdict, deque

from knight_trader import ExchangeClient

log = logging.getLogger("spread_farmer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

# --- MM parameters --------------------------------------------------------
PRINTS_WINDOW       = 60       # rolling window for cv + median
MIN_PRINTS          = 50       # need history before quoting
CV_MAX              = 0.12     # skip volatile names (SCA filter)
MIN_SPREAD_BPS      = 40       # worth quoting only if spread > 40 bps
MIN_INSIDE          = 0.03     # absolute minimum improvement on NBBO
EDGE_PCT            = 0.35     # step inside spread by 15% on each side
BASE_SIZE_PCT       = 0.012    # ~0.8% of capital per quote
MAX_INVENTORY_PCT   = 0.03     # per-symbol inventory cap — lowered from 0.03 after account-wide margin pressure
SKEW_STRENGTH       = 0.5      # inventory skew into quote price
MIN_REFRESH_SEC     = 0.30     # min time between re-quotes per symbol
MAX_SYMBOLS         = 8        # keep focus tight

# --- HILL MR overlay ------------------------------------------------------
HILL_SYMBOL         = "HILL"
MR_EMA_SPAN         = 30
MR_ENTRY_Z          = 1.5
MR_EXIT_Z           = 0.4
MR_SIZE_PCT         = 0.06
MR_MAX_HOLD_SEC     = 900.0    # 15 min hard timeout

# --- Risk -----------------------------------------------------------------
BAILOUT_HALT_EQUITY = 25_000
SANE_MAX_DEV        = 0.03     # reject crosses >3% from rolling median
HARD_BLACKLIST      = {"SCA"}  # fallback in case cv filter lags a blowup

TARGET_TYPES = {"spot", "equity", "equities", "forex", "fx"}


def _num(x, d=0.0):
    try: return float(x)
    except (TypeError, ValueError): return d


def _asset_type(a): return (a.get("asset_type") or a.get("type") or "").lower()
def _sym(a): return a.get("symbol") or a.get("id") or a.get("name")
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


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0: return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _cv(xs):
    if len(xs) < 3: return 0.0
    m = sum(xs) / len(xs)
    if m <= 0: return 0.0
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var) / m


def _sane_vs_median(px, prints, max_dev=SANE_MAX_DEV):
    if px is None or px <= 0: return False
    if not prints: return True
    ref = _median(prints)
    if ref <= 0: return True
    return abs(px - ref) / ref <= max_dev


def _team_equity(t):
    for k in ("total_equity", "equity", "leaderboard_equity", "net_equity"):
        if k in t: return _num(t[k])
    rud = _num(t.get("rud") or t.get("treasury", {}).get("rud"))
    bots_raw = t.get("bots") or {}
    bots_iter = bots_raw.values() if isinstance(bots_raw, dict) else bots_raw
    cap = sum(_num(b.get("capital") or b.get("allocated_capital")) for b in bots_iter)
    return rud + cap


def _bot(t, bid):
    bots = t.get("bots") or t.get("portfolio", {}).get("bots") or {}
    if isinstance(bots, dict): return bots.get(bid) or {}
    return next((b for b in bots if b.get("id") == bid or b.get("bot_id") == bid), {})


def _bot_capital(t, bid):
    b = _bot(t, bid)
    return _num(b.get("capital") or b.get("allocated_capital") or b.get("uncommitted_capital"))


def _bot_positions(t, bid):
    raw = _bot(t, bid).get("positions") or _bot(t, bid).get("inventory") or {}
    out = {}
    if isinstance(raw, dict):
        for s, v in raw.items():
            out[s] = _num(v.get("quantity") or v.get("qty") or v.get("size")) if isinstance(v, dict) else _num(v)
    elif isinstance(raw, list):
        for r in raw:
            s = r.get("symbol") or r.get("asset")
            if s: out[s] = _num(r.get("quantity") or r.get("qty") or r.get("size"))
    return out


class SpreadFarmer:
    def __init__(self):
        self.client = ExchangeClient()
        self.prints = defaultdict(lambda: deque(maxlen=PRINTS_WINDOW))
        self.all_symbols = []
        self.active = []  # filtered symbols currently worth quoting
        self.meta = {}
        self.resting = defaultdict(lambda: {"bid": None, "ask": None, "bid_px": None, "ask_px": None, "ts": 0.0})
        self.last_refresh_assets = 0.0
        self.last_refilter = 0.0
        # HILL MR state
        self.hill_ema = None
        self.hill_var = 0.0       # running variance estimate for z-score
        self.mr_open = None       # ("long"/"short", qty, entry_ts, entry_px)
        self.halted = False

    # --- symbol management -----------------------------------------------
    def refresh_assets(self, now):
        if now - self.last_refresh_assets < 30.0 and self.all_symbols:
            return
        try: assets = self.client.get_assets() or []
        except Exception as exc:
            log.warning("get_assets failed: %s", exc); return
        if isinstance(assets, dict): assets = list(assets.values())
        picked = []
        for a in assets:
            if _asset_type(a) not in TARGET_TYPES or not _tradable(a): continue
            s = _sym(a)
            if s and s not in HARD_BLACKLIST:
                picked.append(s); self.meta[s] = a
        self.all_symbols = picked
        self.last_refresh_assets = now

    def refilter(self, books, now):
        """Every 10s, re-rank symbols by (spread_bps * liquidity) after cv filter."""
        if now - self.last_refilter < 10.0 and self.active:
            return
        ranked = []
        for s in self.all_symbols:
            pxs = list(self.prints[s])
            if len(pxs) < MIN_PRINTS: continue
            if _cv(pxs) > CV_MAX: continue
            book = books.get(s) or {}
            bb, ba = _best(book)
            if not bb or not ba or bb >= ba: continue
            mid = 0.5 * (bb + ba)
            bps = (ba - bb) / mid * 10_000
            if bps < MIN_SPREAD_BPS: continue
            # score = spread_bps weighted by liquidity proxy (print count)
            ranked.append((s, bps * len(pxs), bps))
        ranked.sort(key=lambda x: -x[1])
        new_active = [s for s, _, _ in ranked[:MAX_SYMBOLS]]
        if new_active != self.active:
            log.info("active MM set: %s", [f"{s}({int(bps)}bps)" for s, _, bps in ranked[:MAX_SYMBOLS]])
        self.active = new_active
        self.last_refilter = now

    # --- data ingest ----------------------------------------------------
    def ingest_trades(self, state):
        for t in (state.get("recent_trades") or state.get("trades") or []):
            s = t.get("symbol"); px = _num(t.get("price"))
            if s and px > 0:
                self.prints[s].append(px)
                if s == HILL_SYMBOL:
                    self._update_hill(px)

    def _update_hill(self, px):
        alpha = 2.0 / (MR_EMA_SPAN + 1.0)
        if self.hill_ema is None:
            self.hill_ema = px
            self.hill_var = 0.0
            return
        diff = px - self.hill_ema
        self.hill_ema = self.hill_ema + alpha * diff
        self.hill_var = (1 - alpha) * (self.hill_var + alpha * diff * diff)

    def hill_z(self, mid):
        if self.hill_ema is None or self.hill_var <= 0: return 0.0
        sd = math.sqrt(self.hill_var)
        return (mid - self.hill_ema) / sd if sd > 0 else 0.0

    # --- MM quoting -----------------------------------------------------
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
        mid = 0.5 * (bb + ba)
        edge = max(MIN_INSIDE, EDGE_PCT * spread)

        max_inv_notional = MAX_INVENTORY_PCT * max(cap, 1.0)
        inv_notional = inv * mid
        sat = 0.0 if max_inv_notional <= 0 else max(-1.0, min(1.0, inv_notional / max_inv_notional))
        half = spread / 2.0
        skew = SKEW_STRENGTH * half * sat

        bid_px = round(bb + edge - skew, 4)
        ask_px = round(ba - edge - skew, 4)
        # Self-match + crossing guards
        if bid_px >= ask_px: return None
        if bid_px >= ba or ask_px <= bb: return None

        bid_scale = max(0.0, 1.0 - max(0.0, sat))
        ask_scale = max(0.0, 1.0 + min(0.0, sat))
        raw = (BASE_SIZE_PCT * cap) / mid if mid > 0 else 0.0
        return bid_px, ask_px, round(raw * bid_scale, 4), round(raw * ask_scale, 4), mid

    def quote_symbol(self, sym, book, team, cap, now):
        inv = _bot_positions(team, BOT_ID).get(sym, 0.0)
        target = self.compute_quotes(sym, book, inv, cap)
        rec = self.resting[sym]
        if not target:
            if rec["bid"] or rec["ask"]:
                self.cancel_side(sym, "bid"); self.cancel_side(sym, "ask")
            return
        bid_px, ask_px, bid_q, ask_q, mid = target
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

    # --- HILL MR overlay ------------------------------------------------
    def run_hill_mr(self, books, cap, now):
        book = books.get(HILL_SYMBOL)
        if not book or self.hill_ema is None: return
        bb, ba = _best(book)
        if not bb or not ba: return
        mid = 0.5 * (bb + ba)
        z = self.hill_z(mid)

        # Exit
        if self.mr_open is not None:
            side, qty, entry_ts, entry_px = self.mr_open
            stale = now - entry_ts > MR_MAX_HOLD_SEC
            reverted = (side == "long" and z > -MR_EXIT_Z) or (side == "short" and z < MR_EXIT_Z)
            if stale or reverted:
                flat_px = bb if side == "long" else ba
                if _sane_vs_median(flat_px, self.prints[HILL_SYMBOL], 0.08):
                    try:
                        (self.client.sell if side == "long" else self.client.buy)(HILL_SYMBOL, flat_px, qty)
                        log.info("MR exit %s %s qty=%s @ %s z=%.2f (stale=%s)", HILL_SYMBOL, side, qty, flat_px, z, stale)
                    except Exception as exc: log.warning("MR exit failed: %s", exc)
                    self.mr_open = None
            return

        # Entry
        size = round((MR_SIZE_PCT * cap) / mid, 4)
        if size <= 0: return
        if z < -MR_ENTRY_Z:
            if not _sane_vs_median(ba, self.prints[HILL_SYMBOL]): return
            try:
                self.client.buy(HILL_SYMBOL, ba, size)
                self.mr_open = ("long", size, now, ba)
                log.info("MR LONG %s qty=%s @ %s z=%.2f ema=%.4f", HILL_SYMBOL, size, ba, z, self.hill_ema)
            except Exception as exc: log.warning("MR long failed: %s", exc)
        elif z > MR_ENTRY_Z:
            if not _sane_vs_median(bb, self.prints[HILL_SYMBOL]): return
            try:
                self.client.sell(HILL_SYMBOL, bb, size)
                self.mr_open = ("short", size, now, bb)
                log.info("MR SHORT %s qty=%s @ %s z=%.2f ema=%.4f", HILL_SYMBOL, size, bb, z, self.hill_ema)
            except Exception as exc: log.warning("MR short failed: %s", exc)

    # --- lifecycle ------------------------------------------------------
    def flatten_all(self):
        try: self.client.cancel_all()
        except Exception: pass
        self.resting.clear()
        self.mr_open = None

    def run(self):
        last_team = 0.0
        team = {}
        last_status = 0.0
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                if state.get("competition_state") != "live":
                    if not self.halted: self.flatten_all(); self.halted = True
                    continue
                self.halted = False
                self.refresh_assets(now)
                self.ingest_trades(state)

                if now - last_team > 1.0:
                    try: team = self.client.get_team_state() or {}
                    except Exception as exc: log.warning("get_team_state: %s", exc)
                    last_team = now

                eq = _team_equity(team)
                if eq and eq < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT GUARD: equity %.0f — flattening", eq)
                    self.flatten_all(); time.sleep(1.0); continue

                cap = _bot_capital(team, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}

                self.refilter(books, now)

                # MM pass on active set
                for s in self.active:
                    book = books.get(s) or {}
                    if not book:
                        try: book = self.client.get_book(s) or {}
                        except Exception: book = {}
                    if not book: continue
                    self.quote_symbol(s, book, team, cap, now)

                # HILL MR overlay
                self.run_hill_mr(books, cap, now)

                # Periodic status
                if now - last_status > 30.0:
                    mr = f"{self.mr_open[0]} @ {self.mr_open[3]}" if self.mr_open else "flat"
                    log.info("eq=%.0f cap=%.0f active=%s hill_z=%.2f mr=%s",
                             eq, cap, self.active, self.hill_z(self.hill_ema or 0.0), mr)
                    last_status = now
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(0.5)


if __name__ == "__main__":
    SpreadFarmer().run()
