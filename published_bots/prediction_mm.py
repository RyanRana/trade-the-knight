"""
prediction_mm.py — passive market maker for binary/probability markets.

Discovery (2026-04-18 leaderboard pull):
  - SBLUE/SRED/SYELLOW/SGREEN/SCA show 1100-1200 bps quoted spreads.
  - Only ~4 quoters per book vs 13+ in the spot MM-war zones (HILL/LIVI/PASS).
  - Inside markets sit around 0.40/0.55 with tiny size (1.5) at the touch and
    deep walls (100-200) at 0.40/0.60 — clearly probability markets in [0,1].

Strategy
--------
1. Auto-pick any symbol whose mid sits firmly inside (0.05, 0.95) AND whose
   quoted spread > MIN_SPREAD_BPS — this is the binary/prediction signature.
2. Build a self-anchored fair value from rolling trade prints (median).
3. Quote one tick inside the existing inside market on each side, capped at
   our own MAX_HALF_SPREAD around fair value so we never bid above FV or
   sell below FV.
4. Hard per-symbol inventory cap (probability markets settle to 0 or 1 —
   getting stuck with size at expiry is catastrophic).
5. Skew quotes on inventory: long → cheaper ask, short → cheaper bid.
6. Cancel everything if equity < BAILOUT_HALT_EQUITY.

Why this is orthogonal to spread_farmer
---------------------------------------
spread_farmer's TARGET_TYPES filter excludes non-spot instruments and its
MIN_PRINTS=20 filter rejects thin prediction-market history. This bot
relaxes both for the binary universe only.

Upload as single file. Container injects BOT_ID + EXCHANGE_URL.
"""

import logging
import math
import os
import time
from collections import defaultdict, deque

from knight_trader import ExchangeClient

log = logging.getLogger("prediction_mm")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

# --- Universe selection ---------------------------------------------------
PROB_LO             = 0.05      # mid must be > this to qualify as binary
PROB_HI             = 0.95      # mid must be < this
MIN_SPREAD_BPS      = 200       # uncrowded prediction books only
MAX_SYMBOLS         = 6
SYMBOL_HINTS        = ("S",)    # symbols starting with these letters are likely prediction
HARD_BLACKLIST      = set()

# --- Quote parameters -----------------------------------------------------
TICK                = 0.0001    # min price increment
INSIDE_TICKS        = 1         # step inside best by this many ticks
MAX_HALF_SPREAD     = 0.03      # never quote further than ±3% (in prob units) from FV
MIN_HALF_SPREAD     = 0.005     # always leave ≥0.5% of edge per side
BASE_SIZE_PCT       = 0.004     # 0.4% of capital per quote (probability markets are binary risk)
MAX_INV_PCT         = 0.015     # 1.5% of cap per symbol — strict, settles to 0 or 1
SKEW_STRENGTH       = 1.0       # full inventory skew
MIN_REFRESH_SEC     = 0.5

# --- Fair value -----------------------------------------------------------
PRINTS_WINDOW       = 40
MIN_PRINTS_FV       = 4         # very low — these books are thin
FV_FALLBACK_TO_MID  = True      # if no prints, anchor to mid

# --- Risk -----------------------------------------------------------------
BAILOUT_HALT_EQUITY = 25_000
SANE_MAX_DEV        = 0.20      # in prob units; flag obvious mis-prints
EXCLUDE_TYPES       = {"spot", "equity", "equities", "forex", "fx"}  # let spread_farmer handle these

# Circuit breaker: if the account is over-margined account-wide, stop
# spamming new orders. Other bots' leverage state is opaque to us, so detect
# via consecutive failures and back off.
REJECT_WINDOW_SEC   = 30.0
REJECT_LIMIT        = 4          # rejects in window → pause
REJECT_PAUSE_SEC    = 60.0


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
    s = sorted(xs); n = len(s)
    if n == 0: return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


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


class PredictionMM:
    def __init__(self):
        self.client = ExchangeClient()
        self.prints = defaultdict(lambda: deque(maxlen=PRINTS_WINDOW))
        self.candidates = []  # symbols that *could* be prediction markets
        self.active = []      # currently quoting
        self.meta = {}
        self.resting = defaultdict(lambda: {"bid": None, "ask": None, "bid_px": None, "ask_px": None, "ts": 0.0})
        self.last_refresh_assets = 0.0
        self.last_refilter = 0.0
        self.halted = False
        self.recent_rejects = deque(maxlen=REJECT_LIMIT * 4)
        self.paused_until = 0.0

    def refresh_assets(self, now):
        if now - self.last_refresh_assets < 30.0 and self.candidates:
            return
        try: assets = self.client.get_assets() or []
        except Exception as exc:
            log.warning("get_assets failed: %s", exc); return
        if isinstance(assets, dict): assets = list(assets.values())
        picked = []
        for a in assets:
            if not _tradable(a): continue
            t = _asset_type(a)
            s = _sym(a)
            if not s or s in HARD_BLACKLIST: continue
            # Accept anything that is NOT a clear spot/forex instrument; spread_farmer owns those.
            if t in EXCLUDE_TYPES: continue
            picked.append(s); self.meta[s] = a
        # Also include S-prefixed symbols even if asset_type is unknown (heuristic).
        # Already covered by the EXCLUDE_TYPES gate above.
        self.candidates = picked
        self.last_refresh_assets = now

    def refilter(self, books, now):
        """Pick books whose mid is in (PROB_LO, PROB_HI) and spread > MIN_SPREAD_BPS."""
        if now - self.last_refilter < 8.0 and self.active:
            return
        ranked = []
        for s in self.candidates:
            book = books.get(s)
            if not book: continue
            bb, ba = _best(book)
            if not bb or not ba or bb >= ba: continue
            mid = 0.5 * (bb + ba)
            if mid < PROB_LO or mid > PROB_HI: continue
            bps = (ba - bb) / mid * 10_000
            if bps < MIN_SPREAD_BPS: continue
            ranked.append((s, bps, mid))
        ranked.sort(key=lambda x: -x[1])
        new_active = [s for s, _, _ in ranked[:MAX_SYMBOLS]]
        if new_active != self.active:
            log.info("active prediction set: %s",
                     [f"{s}(mid={m:.3f},{int(b)}bps)" for s, b, m in ranked[:MAX_SYMBOLS]])
        self.active = new_active
        self.last_refilter = now

    def ingest_trades(self, state):
        for t in (state.get("recent_trades") or state.get("trades") or []):
            s = t.get("symbol"); px = _num(t.get("price"))
            if s and 0 < px < 1.0:
                self.prints[s].append(px)

    def fair_value(self, sym, mid):
        pxs = list(self.prints[sym])
        if len(pxs) >= MIN_PRINTS_FV:
            return _median(pxs)
        if FV_FALLBACK_TO_MID:
            return mid
        return None

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
        mid = 0.5 * (bb + ba)
        fv = self.fair_value(sym, mid)
        if fv is None or fv <= 0 or fv >= 1: return None

        # Step inside the existing best by INSIDE_TICKS.
        inside_bid = round(bb + INSIDE_TICKS * TICK, 4)
        inside_ask = round(ba - INSIDE_TICKS * TICK, 4)

        # Bound to FV ± MAX_HALF_SPREAD; never narrower than ± MIN_HALF_SPREAD.
        bid_cap = round(fv - MIN_HALF_SPREAD, 4)
        ask_cap = round(fv + MIN_HALF_SPREAD, 4)
        bid_floor = round(fv - MAX_HALF_SPREAD, 4)
        ask_ceil = round(fv + MAX_HALF_SPREAD, 4)

        bid_px = max(bid_floor, min(inside_bid, bid_cap))
        ask_px = min(ask_ceil, max(inside_ask, ask_cap))

        # Inventory skew — pull both quotes toward unwinding the position.
        max_inv_notional = MAX_INV_PCT * max(cap, 1.0)
        inv_notional = inv * fv
        sat = 0.0 if max_inv_notional <= 0 else max(-1.0, min(1.0, inv_notional / max_inv_notional))
        skew = SKEW_STRENGTH * (MIN_HALF_SPREAD) * sat
        bid_px = round(bid_px - skew, 4)
        ask_px = round(ask_px - skew, 4)

        # Probability bounds + non-cross.
        bid_px = max(0.01, min(bid_px, 0.99 - TICK))
        ask_px = max(0.01 + TICK, min(ask_px, 0.99))
        if bid_px >= ask_px: return None

        # If saturated long, suppress bid; if saturated short, suppress ask.
        bid_scale = max(0.0, 1.0 - max(0.0, sat))
        ask_scale = max(0.0, 1.0 + min(0.0, sat))
        raw = (BASE_SIZE_PCT * cap) / max(fv, 0.05)
        bid_q = round(raw * bid_scale, 4)
        ask_q = round(raw * ask_scale, 4)
        return bid_px, ask_px, bid_q, ask_q, fv

    def _note_attempt(self, oid, exc, now):
        """Track failed sends (None return or exception); trip breaker on cluster."""
        if oid: return
        self.recent_rejects.append(now)
        cutoff = now - REJECT_WINDOW_SEC
        recent = sum(1 for t in self.recent_rejects if t >= cutoff)
        if recent >= REJECT_LIMIT and now >= self.paused_until:
            self.paused_until = now + REJECT_PAUSE_SEC
            log.error("CIRCUIT BREAKER: %d failed sends in %.0fs — pausing %.0fs "
                      "(likely account margin or exchange reject)",
                      recent, REJECT_WINDOW_SEC, REJECT_PAUSE_SEC)

    def quote_symbol(self, sym, book, team, cap, now):
        if now < self.paused_until: return
        inv = _bot_positions(team, BOT_ID).get(sym, 0.0)
        target = self.compute_quotes(sym, book, inv, cap)
        rec = self.resting[sym]
        if not target:
            if rec["bid"] or rec["ask"]:
                self.cancel_side(sym, "bid"); self.cancel_side(sym, "ask")
            return
        bid_px, ask_px, bid_q, ask_q, fv = target
        if now - rec["ts"] < MIN_REFRESH_SEC and rec["bid_px"] == bid_px and rec["ask_px"] == ask_px:
            return
        self.cancel_side(sym, "bid"); self.cancel_side(sym, "ask")
        if bid_q > 0:
            oid = None; exc = None
            try: oid = self.client.buy(sym, bid_px, bid_q)
            except Exception as e: exc = e; log.warning("buy %s @ %s qty=%s failed: %s", sym, bid_px, bid_q, e)
            if oid: rec["bid"], rec["bid_px"] = oid, bid_px
            self._note_attempt(oid, exc, now)
        if now < self.paused_until: return
        if ask_q > 0:
            oid = None; exc = None
            try: oid = self.client.sell(sym, ask_px, ask_q)
            except Exception as e: exc = e; log.warning("sell %s @ %s qty=%s failed: %s", sym, ask_px, ask_q, e)
            if oid: rec["ask"], rec["ask_px"] = oid, ask_px
            self._note_attempt(oid, exc, now)
        rec["ts"] = now

    def flatten_all(self):
        try: self.client.cancel_all()
        except Exception: pass
        self.resting.clear()

    def run(self):
        log.info("prediction_mm online | universe filter: mid∈(%.2f,%.2f) & spread≥%dbps | max_syms=%d",
                 PROB_LO, PROB_HI, MIN_SPREAD_BPS, MAX_SYMBOLS)
        last_team = 0.0; team = {}; last_status = 0.0
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
                    log.error("BAILOUT: equity %.0f — flattening", eq)
                    self.flatten_all(); time.sleep(1.0); continue

                cap = _bot_capital(team, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}
                self.refilter(books, now)

                for s in self.active:
                    book = books.get(s) or {}
                    if not book:
                        try: book = self.client.get_book(s) or {}
                        except Exception: book = {}
                    if not book: continue
                    self.quote_symbol(s, book, team, cap, now)

                if now - last_status > 30.0:
                    log.info("eq=%.0f cap=%.0f active=%s candidates=%d",
                             eq, cap, self.active, len(self.candidates))
                    last_status = now
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(0.5)


def _supervised():
    """Outer auto-restart loop. Crashes in run() relaunch a fresh instance."""
    backoff = 1.0
    while True:
        try:
            PredictionMM().run()
            log.warning("run() returned cleanly — restarting in %.1fs", backoff)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.exception("FATAL in run(): %s — restarting in %.1fs", exc, backoff)
        time.sleep(backoff)
        backoff = min(60.0, backoff * 2)


if __name__ == "__main__":
    _supervised()
