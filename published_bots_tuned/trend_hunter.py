"""AUTO-TUNED by backtest/tune.py
bot: trend_hunter
trials: 60
baseline_score: 0.00
best_score: 17638.25
improvement: +17638.25
best_overrides: {'CV_MIN': 0.05, 'CV_MAX': 0.2, 'MAX_SPREAD_BPS': 300, 'ENTRY_Z': 0.8, 'EXIT_Z': 0.1, 'EMA_SPAN': 60, 'MAX_POS_PCT': 0.1, 'STOP_ATR': 1.0, 'TAKE_ATR': 4.0, 'MAX_HOLD_SEC': 300.0}

Generated copy — edit the source in published_bots/ and re-run tune.py instead.
"""
"""
trend_hunter.py — focused breakout / momentum on volatile spot names.

Hedge against the implicit mean-reversion bias of cross_section_engine and
spread_farmer. Those bots make money when prices revert. This one makes
money when they break out. Running all three gives regime coverage.

Selection (refreshed every 10s):
  cv(40-tick)  between CV_MIN and CV_MAX  — needs enough vol to trend,
                                            not so much it's blown up (SCA)
  spread_bps   <= MAX_SPREAD_BPS          — can't aggress through wide books
  prints       >= MIN_PRINTS

Signal per symbol:
  z = (price - EMA40) / rolling_sigma
  long  when z >=  ENTRY_Z  AND 5-tick return > 0  (direction confirmation)
  short when z <= -ENTRY_Z  AND 5-tick return < 0

Risk:
  ATR-based stop-loss at STOP_ATR * atr from entry
  Profit target at TAKE_ATR * atr OR reverse signal
  Hard time stop at MAX_HOLD_SEC
  Per-symbol notional cap = MAX_POS_PCT * cap
  Gross book cap = MAX_GROSS_PCT * cap
  Bailout floor at BAILOUT_HALT_EQUITY

Upload as single file. Container injects BOT_ID + EXCHANGE_URL.
"""

import logging
import math
import os
import time
from collections import defaultdict, deque

from knight_trader import ExchangeClient

log = logging.getLogger("trend_hunter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

# --- Selection ------------------------------------------------------------
PRINTS_WINDOW       = 40
MIN_PRINTS          = 25
CV_MIN              = 0.05       # floor — nothing to trend below this
CV_MAX              = 0.2       # ceiling — above this is chaos (SCA territory)
MAX_SPREAD_BPS      = 300
MAX_SYMBOLS         = 6

# --- Signal ---------------------------------------------------------------
EMA_SPAN            = 60
MOMENTUM_LOOKBACK   = 5          # ticks for direction confirmation
ENTRY_Z             = 0.8
EXIT_Z              = 0.1        # flatten when signal decays

# --- Risk -----------------------------------------------------------------
MAX_POS_PCT         = 0.1
MAX_GROSS_PCT       = 0.30
STOP_ATR            = 1.0
TAKE_ATR            = 4.0
MAX_HOLD_SEC        = 300.0
MIN_REBAL_SEC       = 2.0
BAILOUT_HALT_EQUITY = 25_000
SANE_MAX_DEV        = 0.04       # reject crosses >4% from rolling median
HARD_BLACKLIST      = {"SCA"}

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
    s = sorted(xs); n = len(s)
    if n == 0: return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _mean(xs): return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2: return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _cv(xs):
    m = _mean(xs)
    if m <= 0 or len(xs) < 3: return 0.0
    return _std(xs) / m


def _atr(xs, period=14):
    """Rough ATR from print deltas (no high/low available on public tape)."""
    if len(xs) < period + 1: return 0.0
    diffs = [abs(xs[i] - xs[i + 1]) for i in range(min(period, len(xs) - 1))]
    return _mean(diffs) if diffs else 0.0


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


class TrendHunter:
    def __init__(self):
        self.client = ExchangeClient()
        self.prints = defaultdict(lambda: deque(maxlen=PRINTS_WINDOW))
        self.all_symbols = []
        self.active = []
        self.ema = {}            # sym -> EMA
        self.open_pos = {}       # sym -> {side, qty, entry_px, entry_ts, atr}
        self.last_trade_ts = {}  # sym -> last entry/exit monotonic time
        self.last_refresh_assets = 0.0
        self.last_refilter = 0.0
        self.halted = False

    def refresh_assets(self, now):
        if now - self.last_refresh_assets < 30.0 and self.all_symbols: return
        try: assets = self.client.get_assets() or []
        except Exception as exc:
            log.warning("get_assets failed: %s", exc); return
        if isinstance(assets, dict): assets = list(assets.values())
        self.all_symbols = [_sym(a) for a in assets
                            if _asset_type(a) in TARGET_TYPES and _tradable(a)
                            and _sym(a) and _sym(a) not in HARD_BLACKLIST]
        self.last_refresh_assets = now

    def refilter(self, books, now):
        if now - self.last_refilter < 10.0 and self.active: return
        ranked = []
        for s in self.all_symbols:
            pxs = list(self.prints[s])
            if len(pxs) < MIN_PRINTS: continue
            cv = _cv(pxs)
            if cv < CV_MIN or cv > CV_MAX: continue
            book = books.get(s) or {}
            bb, ba = _best(book)
            if not bb or not ba or bb >= ba: continue
            mid = 0.5 * (bb + ba)
            bps = (ba - bb) / mid * 10_000
            if bps > MAX_SPREAD_BPS: continue
            # score = cv * liquidity (want volatile AND frequently traded)
            ranked.append((s, cv * len(pxs), cv, bps))
        ranked.sort(key=lambda x: -x[1])
        new_active = [s for s, _, _, _ in ranked[:MAX_SYMBOLS]]
        if new_active != self.active:
            log.info("active trend set: %s", [f"{s}(cv={cv:.2f},{int(bps)}bps)" for s, _, cv, bps in ranked[:MAX_SYMBOLS]])
        self.active = new_active
        self.last_refilter = now

    def ingest_trades(self, state):
        for t in (state.get("recent_trades") or state.get("trades") or []):
            s = t.get("symbol"); px = _num(t.get("price"))
            if s and px > 0:
                self.prints[s].append(px)
                self._update_ema(s, px)

    def _update_ema(self, sym, px):
        alpha = 2.0 / (EMA_SPAN + 1.0)
        prev = self.ema.get(sym)
        self.ema[sym] = px if prev is None else alpha * px + (1 - alpha) * prev

    def _signal(self, sym, mid):
        ema = self.ema.get(sym)
        if ema is None: return 0.0, 0.0
        pxs = list(self.prints[sym])
        if len(pxs) < MIN_PRINTS: return 0.0, 0.0
        sd = _std(pxs)
        if sd <= 0: return 0.0, 0.0
        z = (mid - ema) / sd
        # momentum confirmation: recent N-tick return (newest first in deque)
        recent = pxs[:MOMENTUM_LOOKBACK + 1]
        if len(recent) < 2: return z, 0.0
        mom = (recent[0] - recent[-1]) / recent[-1] if recent[-1] else 0.0
        return z, mom

    def _gross_notional(self):
        total = 0.0
        for sym, p in self.open_pos.items():
            pxs = list(self.prints[sym])
            ref = _median(pxs) if pxs else p["entry_px"]
            total += abs(p["qty"]) * ref
        return total

    def _maybe_exit(self, sym, book, now):
        pos = self.open_pos.get(sym)
        if not pos: return
        bb, ba = _best(book)
        if not bb or not ba: return
        mid = 0.5 * (bb + ba)
        side, qty, entry_px, entry_ts, atr = pos["side"], pos["qty"], pos["entry_px"], pos["entry_ts"], pos["atr"]
        z, _ = self._signal(sym, mid)

        stale = now - entry_ts > MAX_HOLD_SEC
        decayed = abs(z) < EXIT_Z
        if side == "long":
            pnl_px = mid - entry_px
            hit_stop = pnl_px <= -STOP_ATR * atr
            hit_take = pnl_px >= TAKE_ATR * atr
            reversed_ = z <= -EXIT_Z
        else:
            pnl_px = entry_px - mid
            hit_stop = pnl_px <= -STOP_ATR * atr
            hit_take = pnl_px >= TAKE_ATR * atr
            reversed_ = z >= EXIT_Z

        reason = None
        if hit_stop: reason = "stop"
        elif hit_take: reason = "take"
        elif reversed_: reason = "reverse"
        elif stale: reason = "stale"
        elif decayed: reason = "decay"
        if not reason: return

        flat_px = bb if side == "long" else ba
        # On stop, accept wider slippage — we NEED to get out.
        max_dev = 0.10 if reason == "stop" else SANE_MAX_DEV
        if not _sane_vs_median(flat_px, self.prints[sym], max_dev):
            log.warning("EXIT %s deferred (%s outside ±%.0f%% of median)", sym, flat_px, max_dev * 100)
            return
        try:
            (self.client.sell if side == "long" else self.client.buy)(sym, flat_px, qty)
            log.info("EXIT %s %s qty=%s @ %s reason=%s pnl_px=%.4f", sym, side, qty, flat_px, reason, pnl_px)
        except Exception as exc:
            log.warning("exit %s failed: %s", sym, exc); return
        self.open_pos.pop(sym, None)
        self.last_trade_ts[sym] = now

    def _maybe_enter(self, sym, book, cap, now):
        if sym in self.open_pos: return
        if now - self.last_trade_ts.get(sym, 0.0) < MIN_REBAL_SEC: return
        if self._gross_notional() > MAX_GROSS_PCT * cap: return
        bb, ba = _best(book)
        if not bb or not ba: return
        mid = 0.5 * (bb + ba)
        z, mom = self._signal(sym, mid)
        go_long = z >= ENTRY_Z and mom > 0
        go_short = z <= -ENTRY_Z and mom < 0
        if not (go_long or go_short): return

        atr = _atr(list(self.prints[sym]))
        if atr <= 0: return
        notional = MAX_POS_PCT * cap
        qty = round(notional / mid, 4)
        if qty <= 0: return
        exec_px = ba if go_long else bb
        if not _sane_vs_median(exec_px, self.prints[sym]): return
        side = "long" if go_long else "short"
        try:
            (self.client.buy if go_long else self.client.sell)(sym, exec_px, qty)
        except Exception as exc:
            log.warning("entry %s failed: %s", sym, exc); return
        self.open_pos[sym] = {"side": side, "qty": qty, "entry_px": exec_px, "entry_ts": now, "atr": atr}
        self.last_trade_ts[sym] = now
        log.info("ENTER %s %s qty=%s @ %s z=%.2f mom=%.4f atr=%.4f", sym, side, qty, exec_px, z, mom, atr)

    def flatten_all(self):
        try: self.client.cancel_all()
        except Exception: pass
        self.open_pos.clear()

    def run(self):
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
                    log.error("BAILOUT GUARD: equity %.0f — flattening", eq)
                    self.flatten_all(); time.sleep(1.0); continue

                cap = _bot_capital(team, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}
                self.refilter(books, now)

                # Always manage exits for anything open, even if dropped from active
                for sym in list(self.open_pos.keys()):
                    book = books.get(sym) or {}
                    if book: self._maybe_exit(sym, book, now)

                # Entries only on active set
                for sym in self.active:
                    book = books.get(sym) or {}
                    if book: self._maybe_enter(sym, book, cap, now)

                if now - last_status > 30.0:
                    log.info("eq=%.0f cap=%.0f active=%s open=%s gross=%.0f",
                             eq, cap, self.active,
                             {s: (p["side"], p["qty"]) for s, p in self.open_pos.items()},
                             self._gross_notional())
                    last_status = now
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(0.5)


if __name__ == "__main__":
    TrendHunter().run()
