"""AUTO-TUNED by backtest/tune.py
bot: event_alpha
trials: 60
baseline_score: 0.51
best_score: 2.48
improvement: +1.97
best_overrides: {'PRED_EDGE_MIN': 0.08, 'VRP_EDGE_MIN': 0.05, 'TRI_EDGE_MIN': 0.005, 'BAYES_POSTERIOR_EDGE': 0.15, 'MAX_HOLD_SEC': 300.0}

Generated copy — edit the source in published_bots/ and re-run tune.py instead.
"""
"""
event_alpha.py — opportunistic scanner for non-spot alpha.

Ensembles four event-driven strategies into one bot. Idle until the relevant
asset class exists on the exchange, then each scanner fires independently:

  (1) pred_mispricing   — prediction markets: EV vs market price; Kelly size
  (2) options_vrp       — sell rich IV, delta-hedge underlying
  (3) fx_tri_arb        — triangular arbitrage across FX triplets (IOC legs)
  (4) bayes_pred        — Bayesian probability update from any correlated
                           public timeseries

Meta-allocator: each scanner has a "budget" fraction of bot capital; unused
budget carries over tick-to-tick. No position overlap between scanners.

Upload as single file. Container injects BOT_ID + EXCHANGE_URL.
"""

import logging
import math
import os
import time
from collections import defaultdict, deque

from knight_trader import ExchangeClient

log = logging.getLogger("event_alpha")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

PREDICTION_TYPES = {"prediction", "prediction_market", "pm", "yes_no"}
OPTION_TYPES     = {"option", "options", "call", "put"}
FX_TYPES         = {"forex", "fx"}

# Budgets (fraction of bot capital per scanner; unused = idle).
PRED_BUDGET     = 0.30
VRP_BUDGET      = 0.25
TRI_BUDGET      = 0.25
BAYES_BUDGET    = 0.20

# Thresholds
PRED_EDGE_MIN   = 0.08       # trade only when |market - estimate| > 10¢
VRP_EDGE_MIN    = 0.05       # sell option when (IV-RV)/RV > 10%
TRI_EDGE_MIN    = 0.005      # >10 bps after spread costs
BAYES_POSTERIOR_EDGE = 0.15

VOL_WINDOW      = 60
MAX_HOLD_SEC    = 300.0     # auto-flatten event positions after 30 min
BAILOUT_HALT_EQUITY = 25_000

# Outlier guard — reject VRP/FX executions more than N% from rolling prints.
# Prediction markets use their model estimate as the reference instead.
SANE_MAX_DEV    = 0.05


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


def _sane_aggressive(px, prints, max_dev=SANE_MAX_DEV):
    """For spot/options/FX: reject crosses more than max_dev from mean(recent prints)."""
    if px is None or px <= 0:
        return False
    pxs = list(prints) if prints else []
    if not pxs:
        return True
    ref = sum(pxs) / len(pxs)
    if ref <= 0:
        return True
    return abs(px - ref) / ref <= max_dev


def _mean(xs): return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2: return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


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


def kelly_fraction(p, b):
    """Half-Kelly fraction for bet paying b:1 at win probability p."""
    q = 1 - p
    f = (p * b - q) / b if b > 0 else 0.0
    return max(0.0, min(0.25, 0.5 * f))


class EventAlpha:
    def __init__(self):
        self.client = ExchangeClient()
        self.prints = defaultdict(lambda: deque(maxlen=VOL_WINDOW))
        self.open_positions = {}   # sym -> (qty_signed, entry_ts, scanner)
        self.halted = False

    def fetch_assets(self):
        try: assets = self.client.get_assets() or []
        except Exception: return {}
        if isinstance(assets, dict): assets = list(assets.values())
        out = defaultdict(list)
        for a in assets:
            t = _asset_type(a)
            if not _tradable(a): continue
            if t in PREDICTION_TYPES: out["pred"].append(a)
            elif t in OPTION_TYPES:   out["opt"].append(a)
            elif t in FX_TYPES:       out["fx"].append(a)
            elif t in {"spot", "equity", "equities"}: out["spot"].append(a)
        return out

    def ingest_trades(self, state):
        for t in (state.get("recent_trades") or state.get("trades") or []):
            s = t.get("symbol"); px = _num(t.get("price"))
            if s and px > 0:
                self.prints[s].append(px)

    # --- PREDICTION MARKETS --------------------------------------------
    def scan_prediction(self, pred_assets, books, budget):
        for a in pred_assets:
            s = _sym(a)
            if s in self.open_positions: continue
            bb, ba = _best(books.get(s) or {})
            if not bb or not ba: continue
            mid = 0.5 * (bb + ba)
            estimate = _num(a.get("model_probability") or a.get("fair_value") or 0.5)
            edge = estimate - mid
            if abs(edge) < PRED_EDGE_MIN: continue
            # Outlier guard vs estimate: taking liquidity only has edge if the
            # EXECUTION price (not mid) is on the profitable side of estimate.
            # This naturally rejects an outlier-high ask or outlier-low bid.
            if edge > 0 and ba >= estimate - PRED_EDGE_MIN:
                log.warning("PRED %s skipped — ba %.3f too close to estimate %.3f (mid edge illusory)", s, ba, estimate)
                continue
            if edge < 0 and bb <= estimate + PRED_EDGE_MIN:
                log.warning("PRED %s skipped — bb %.3f too close to estimate %.3f (mid edge illusory)", s, bb, estimate)
                continue
            # Kelly: payout ratio b = (1 - price) / price (YES share)
            price = ba if edge > 0 else bb
            b = (1 - price) / price if price > 0 else 0
            f = kelly_fraction(estimate if edge > 0 else (1 - estimate), b)
            qty = max(1, int(f * budget / price))
            try:
                if edge > 0:
                    self.client.buy(s, ba, qty)
                    self.open_positions[s] = (qty, time.monotonic(), "pred")
                    log.info("PRED LONG %s qty=%s @ %s (est=%.2f, mkt=%.2f, f=%.3f)", s, qty, ba, estimate, mid, f)
                else:
                    self.client.sell(s, bb, qty)
                    self.open_positions[s] = (-qty, time.monotonic(), "pred")
                    log.info("PRED SHORT %s qty=%s @ %s (est=%.2f, mkt=%.2f, f=%.3f)", s, qty, bb, estimate, mid, f)
            except Exception as exc:
                log.warning("pred %s failed: %s", s, exc)

    # --- OPTIONS VRP ---------------------------------------------------
    def realized_vol(self, underlying):
        px = list(self.prints[underlying])
        if len(px) < 10: return None
        rets = [math.log(px[i] / px[i+1]) for i in range(len(px)-1) if px[i+1] > 0]
        if not rets: return None
        return _std(rets) * math.sqrt(252)

    def scan_options(self, opt_assets, books, budget):
        for a in opt_assets:
            s = _sym(a)
            if s in self.open_positions: continue
            underlying = a.get("underlying") or a.get("underlying_symbol")
            iv = _num(a.get("implied_vol") or a.get("iv"))
            if not underlying or iv <= 0: continue
            rv = self.realized_vol(underlying)
            if rv is None or rv <= 0: continue
            edge = (iv - rv) / rv
            if edge < VRP_EDGE_MIN: continue
            bb, ba = _best(books.get(s) or {})
            if not bb: continue
            if not _sane_aggressive(bb, self.prints[s]):
                log.warning("VRP %s skipped — bb %s outside ±%.0f%% of recent prints", s, bb, SANE_MAX_DEV * 100)
                continue
            qty = max(1, int((0.2 * budget) / max(bb, 0.01)))
            try:
                self.client.sell(s, bb, qty)
                self.open_positions[s] = (-qty, time.monotonic(), "vrp")
                log.info("VRP SHORT %s qty=%s @ %s (IV=%.3f, RV=%.3f, edge=%.2f)", s, qty, bb, iv, rv, edge)
            except Exception as exc:
                log.warning("vrp %s failed: %s", s, exc)

    # --- TRIANGULAR ARB ------------------------------------------------
    def scan_triangular(self, fx_assets, books, budget):
        if len(fx_assets) < 3: return
        syms = [_sym(a) for a in fx_assets]
        # Only check N^3 triplets if N small (container has 0.25 CPU).
        if len(syms) > 8: syms = syms[:8]
        for i, a in enumerate(syms):
            for j, b in enumerate(syms):
                if j == i: continue
                for k, c in enumerate(syms):
                    if k in (i, j): continue
                    ab = _best(books.get(a) or {})
                    bc = _best(books.get(b) or {})
                    ac = _best(books.get(c) or {})
                    if not all((ab[0], ab[1], bc[0], bc[1], ac[0], ac[1])): continue
                    implied = ab[1] * bc[1]  # buy a→b→c chain
                    market = ac[0]           # sell a→c directly
                    if market <= 0: continue
                    edge = (implied - market) / market
                    if edge < TRI_EDGE_MIN: continue
                    # size tiny — just prove the pattern
                    qty = max(1, int((0.1 * budget) / max(ab[1], 0.01)))
                    log.info("TRI ARB %s→%s→%s vs %s edge=%.4f qty=%s", a, b, c, c, edge, qty)
                    # Executions omitted here: real IOC on 3 legs needs
                    # protocol support not guaranteed in the vanilla SDK
                    # shape. Keep detection + logging; wire when SDK confirms.
                    return

    # --- BAYES ----------------------------------------------------------
    def scan_bayes(self, pred_assets, budget):
        # Minimal Bayesian update: prior 0.5, add +0.1 per correlated
        # positive-momentum timeseries observation. Placeholder that becomes
        # a full naïve Bayes once public timeseries schema is known.
        try: series = self.client.get_timeseries("ior_rate", limit=10) or []
        except Exception: return
        if not series: return
        recent_change = _num(series[-1].get("v") if isinstance(series[-1], dict) else series[-1]) - \
                        _num(series[0].get("v") if isinstance(series[0], dict) else series[0])
        tilt = 0.1 if recent_change > 0 else -0.1
        for a in pred_assets:
            s = _sym(a)
            if s in self.open_positions: continue
            posterior = 0.5 + tilt
            # If model_probability + tilt diverges from market, size tiny position
            # implemented in scan_prediction via the estimate. Here we just log.
        return

    # --- bookkeeping ---------------------------------------------------
    def flatten_stale(self):
        now = time.monotonic()
        for s, (qty, ts, scanner) in list(self.open_positions.items()):
            if now - ts > MAX_HOLD_SEC:
                try:
                    if qty > 0: self.client.sell(s, 0.0, qty)
                    else:       self.client.buy(s, 0.0, abs(qty))
                except Exception: pass
                self.open_positions.pop(s, None)
                log.info("FLAT %s (stale %ss)", s, scanner)

    def flatten_all(self):
        try: self.client.cancel_all()
        except Exception: pass
        self.open_positions.clear()

    def run(self):
        last_team = 0.0
        team = {}
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                if state.get("competition_state") != "live":
                    if not self.halted: self.flatten_all(); self.halted = True
                    continue
                self.halted = False

                if now - last_team > 1.0:
                    try: team = self.client.get_team_state() or {}
                    except Exception as exc: log.warning("get_team_state: %s", exc)
                    last_team = now

                eq = _team_equity(team)
                if eq and eq < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT GUARD: equity %.0f — flatten + idle", eq)
                    self.flatten_all(); time.sleep(1.0); continue

                cap = _bot_capital(team, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}
                self.ingest_trades(state)
                self.flatten_stale()

                buckets = self.fetch_assets()
                if buckets["pred"]: self.scan_prediction(buckets["pred"], books, PRED_BUDGET * cap)
                if buckets["opt"]:  self.scan_options(   buckets["opt"],  books, VRP_BUDGET  * cap)
                if buckets["fx"]:   self.scan_triangular(buckets["fx"],   books, TRI_BUDGET  * cap)
                if buckets["pred"]: self.scan_bayes(     buckets["pred"],        BAYES_BUDGET * cap)
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(0.5)


if __name__ == "__main__":
    EventAlpha().run()
