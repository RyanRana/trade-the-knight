"""AUTO-TUNED by backtest/tune.py
bot: cross_section_engine
trials: 60
baseline_score: -10621.75
best_score: 7236.55
improvement: +17858.30
best_overrides: {'TOP_K_FRACTION': 0.2, 'GROSS_EXPOSURE_PCT': 0.15, 'MAX_POS_PCT': 0.08, 'MIN_SCORE': 0.15, 'REBALANCE_SEC': 60.0, 'PRINTS_WINDOW': 30}

Generated copy — edit the source in published_bots/ and re-run tune.py instead.
"""
"""
cross_section_engine.py — dollar-neutral portfolio engine.

Ensembles four spot-focused alpha signals into one bot:

  (1) pairs_arb         — pairwise price-ratio z-score (mean reversion)
  (2) multi_stat_arb    — cross-sectional return z-score (reversion)
  (3) residual_momentum — return after regressing out equal-weighted market
  (4) sector_rotation   — relative ranking vs peers

Each signal produces a per-symbol score in [-1, 1]. Scores are averaged
(equal-weighted ensemble), then the top-K go long and bottom-K go short,
sized inversely to recent variance. Dollar-neutral book.

Does nothing for the first ~30 prints per symbol (needs history). After
warm-up it rebalances every RUN_EVERY_TICKS (soft clock via state updates).

Upload as single file. Container injects BOT_ID + EXCHANGE_URL.
"""

import logging
import math
import os
import time
from collections import defaultdict, deque

from knight_trader import ExchangeClient

log = logging.getLogger("xsec")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

PRINTS_WINDOW       = 30
MIN_HISTORY         = 20
REBALANCE_SEC       = 60.0
TOP_K_FRACTION      = 0.2     # top/bottom third of scored symbols
GROSS_EXPOSURE_PCT  = 0.15     # max gross (long + short notional) / capital — lowered from 0.25 after live margin rejections showed account-wide gross $570k vs $301k cap
MAX_POS_PCT         = 0.08     # per-symbol cap — lowered from 0.06 to match new gross cap
MAX_SHORT_GROSS_PCT = 0.10     # naked shorts 1.5x margin'd — lowered from 0.15
MIN_SCORE           = 0.15     # absolute score needed to take a position
BAILOUT_HALT_EQUITY = 25_000

# Risk-off: if current gross exposure already exceeds this fraction of the
# bot's gross cap, override desired→0 for every symbol so every order is
# position-reducing. Prevents the "margin-locked" state where the bot keeps
# trying to add exposure while the exchange rejects every new order.
RISK_OFF_FRAC_OF_GROSS_CAP = 0.90

# Outlier guard — reject aggressive crosses more than N% from rolling trade-print mean.
SANE_MAX_DEV        = 0.05

# Symbols excluded from both scoring and trading. SCA gapped >60% three times
# in 125 snapshots; its VR(5)=0.54 "mean-revert" stat is an artifact of those
# gaps. Including it in cross-sectional z-scores would inflate the std and mute
# every other name's signal. Existing SCA inventory must be flattened manually.
SKIP_TRADE_SYMBOLS = {"SCA"}

# Per-symbol wide-spread thresholds (bps). Derived from p90 of observed spread
# distribution. When current spread exceeds threshold we stand aside — the name
# is either illiquid or dislocated and aggressive crosses leak far too much.
SPREAD_WIDE_BPS = {
    "HILL": 180,
    "LIVI": 160,
    "PASS": 185,
    "QFC":  383,
    "RITE": 223,
    "RUX":  1700,  # RUX natively trades wider; use p90 not p50
    "SCAR": 320,
    "SCIX": 365,
    "YARD": 280,   # high spread→move corr (+0.52) — stay out when wide
}
SPREAD_WIDE_DEFAULT = 400  # fallback for symbols we haven't calibrated

SPOT_TYPES = {"spot", "equity", "equities", "forex", "fx"}


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
    """Reject execution prices more than max_dev from mean(recent prints).
    Returns True if no history (don't block first trade)."""
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


def _zscore(x, xs):
    sd = _std(xs)
    return 0.0 if sd == 0 else (x - _mean(xs)) / sd


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


class Engine:
    def __init__(self):
        self.client = ExchangeClient()
        self.prints = defaultdict(lambda: deque(maxlen=PRINTS_WINDOW))
        self.symbols = []
        self.last_refresh = 0.0
        self.last_rebalance = 0.0
        self.target = {}  # sym -> target signed notional

    def refresh_symbols(self, now):
        if now - self.last_refresh < 30.0 and self.symbols:
            return
        try: assets = self.client.get_assets() or []
        except Exception as exc:
            log.warning("get_assets failed: %s", exc); return
        if isinstance(assets, dict): assets = list(assets.values())
        self.symbols = [
            _sym(a) for a in assets
            if _asset_type(a) in SPOT_TYPES
            and _tradable(a)
            and _sym(a)
            and _sym(a) not in SKIP_TRADE_SYMBOLS
        ]
        self.last_refresh = now
        log.info("tracking %d symbols (skipped %s)", len(self.symbols), sorted(SKIP_TRADE_SYMBOLS))

    def ingest_trades(self, state):
        for t in (state.get("recent_trades") or state.get("trades") or []):
            s = t.get("symbol"); px = _num(t.get("price"))
            if s and px > 0:
                self.prints[s].append(px)

    # --- signals ---------------------------------------------------------
    def signal_cross_section(self):
        """multi_stat_arb: long negative recent returns, short positive (reversion)."""
        rets = {}
        for s in self.symbols:
            px = list(self.prints[s])
            if len(px) < MIN_HISTORY: continue
            # px[0] = oldest, px[-1] = newest. Return is (end - start) / start.
            rets[s] = (px[-1] - px[0]) / px[0] if px[0] else 0.0
        if len(rets) < 3: return {}
        vals = list(rets.values())
        return {s: -max(-2.0, min(2.0, _zscore(r, vals))) / 2.0 for s, r in rets.items()}

    def signal_residual_momentum(self):
        """residual_momentum: return minus equal-weighted market return (last 10 prints)."""
        rets_10 = {}
        for s in self.symbols:
            px = list(self.prints[s])
            if len(px) < MIN_HISTORY: continue
            recent = px[-10:]  # newest 10 (deque appends right-ward)
            rets_10[s] = (recent[-1] - recent[0]) / recent[0] if recent[0] else 0.0
        if len(rets_10) < 3: return {}
        mkt = _mean(list(rets_10.values()))
        resid = {s: r - mkt for s, r in rets_10.items()}
        vals = list(resid.values())
        return {s: max(-2.0, min(2.0, _zscore(r, vals))) / 2.0 for s, r in resid.items()}

    def signal_pairs(self):
        """pairs_arb: score each symbol by avg z-score of its ratio vs peers."""
        price_hist = {s: list(self.prints[s]) for s in self.symbols if len(self.prints[s]) >= MIN_HISTORY}
        if len(price_hist) < 2: return {}
        per_sym_scores = defaultdict(list)
        syms = list(price_hist.keys())
        for i, a in enumerate(syms):
            for b in syms[i + 1:]:
                ha, hb = price_hist[a], price_hist[b]
                n = min(len(ha), len(hb))
                ratios = [ha[k] / hb[k] for k in range(n) if hb[k]]
                if len(ratios) < MIN_HISTORY: continue
                z = _zscore(ratios[-1], ratios)  # current ratio vs historical distribution
                # positive z → a is rich → short a, long b
                clipped = max(-2.0, min(2.0, z)) / 2.0
                per_sym_scores[a].append(-clipped)
                per_sym_scores[b].append(clipped)
        return {s: _mean(v) for s, v in per_sym_scores.items() if v}

    def signal_rotation(self):
        """sector_rotation: normalized rank by full-window return (long top, short bottom)."""
        rets = {}
        for s in self.symbols:
            px = list(self.prints[s])
            if len(px) < MIN_HISTORY: continue
            rets[s] = (px[-1] - px[0]) / px[0] if px[0] else 0.0
        if len(rets) < 3: return {}
        ranked = sorted(rets.items(), key=lambda kv: kv[1])
        n = len(ranked)
        # ranked[0] = biggest loser (score -1), ranked[-1] = biggest winner (score +1).
        # Ensemble inverts this into a reversal signal.
        return {s: (idx / (n - 1) * 2 - 1) if n > 1 else 0 for idx, (s, _) in enumerate(ranked)}

    def ensemble(self):
        signals = [
            self.signal_cross_section(),
            self.signal_residual_momentum(),
            self.signal_pairs(),
            # rotation: momentum direction → reversal (invert sign to match rest)
            {s: -v for s, v in self.signal_rotation().items()},
        ]
        # equal-weight average across available signals per symbol
        agg = defaultdict(list)
        for sig in signals:
            for s, v in sig.items():
                agg[s].append(v)
        return {s: _mean(v) for s, v in agg.items() if v}

    # --- execution -------------------------------------------------------
    def rebalance(self, team, books, cap, now):
        if now - self.last_rebalance < REBALANCE_SEC: return
        self.last_rebalance = now
        # Cancel any of our resting orders before sizing fresh deltas.
        # Without this, aggressive limits that didn't fill keep accumulating
        # against current positions that already reflect partial fills.
        try: self.client.cancel_all()
        except Exception as exc: log.warning("cancel_all: %s", exc)
        scores = self.ensemble()
        if not scores:
            log.info("no ensemble scores yet (need ≥%d prints per sym)", MIN_HISTORY)
            return
        # top/bottom K
        ranked = sorted(scores.items(), key=lambda kv: kv[1])
        k = max(1, int(len(ranked) * TOP_K_FRACTION))
        shorts = [(s, v) for s, v in ranked[:k] if v <= -MIN_SCORE]
        longs  = [(s, v) for s, v in ranked[-k:] if v >= MIN_SCORE]
        if not longs and not shorts:
            log.info("ensemble weak (top=%s, bot=%s)", ranked[-1] if ranked else None, ranked[0] if ranked else None)
            return

        # Dollar-neutral target, but bound the short sleeve separately —
        # naked shorts are charged 1.5× market so we keep the short book small.
        gross = GROSS_EXPOSURE_PCT * cap
        side_gross = gross / 2.0
        short_side = min(side_gross, MAX_SHORT_GROSS_PCT * cap)
        long_per  = side_gross / len(longs) if longs else 0.0
        short_per = short_side / len(shorts) if shorts else 0.0

        current = _bot_positions(team, BOT_ID)
        desired = defaultdict(float)
        for s, _ in longs:  desired[s] += long_per
        for s, _ in shorts: desired[s] -= short_per

        # Risk-off check: if we're already near/over the gross cap from existing
        # positions, every new opening order will be margin-rejected. Override
        # targets to zero so the bot only places reducing orders.
        cur_gross_notional = 0.0
        for s_, q_ in current.items():
            if s_ == "RUD": continue
            b_ = books.get(s_) or {}
            bb_, ba_ = _best(b_)
            if bb_ and ba_:
                cur_gross_notional += abs(q_) * 0.5 * (bb_ + ba_)
        risk_off_threshold = RISK_OFF_FRAC_OF_GROSS_CAP * GROSS_EXPOSURE_PCT * cap
        risk_off = cur_gross_notional > risk_off_threshold
        if risk_off:
            log.warning(
                "RISK-OFF: gross $%.0f > $%.0f (%.0f%% of %.0f%% gross cap on $%.0f) — flatten-only",
                cur_gross_notional, risk_off_threshold,
                RISK_OFF_FRAC_OF_GROSS_CAP * 100, GROSS_EXPOSURE_PCT * 100, cap,
            )
            desired = defaultdict(float)  # target zero everywhere; deltas become -cur_qty

        for s in set(list(desired.keys()) + list(current.keys())):
            if s == "RUD": continue
            if s in SKIP_TRADE_SYMBOLS:
                continue
            book = books.get(s) or {}
            bb, ba = _best(book)
            if not bb or not ba: continue
            mid = 0.5 * (bb + ba)
            spread_bps = 10_000 * (ba - bb) / mid if mid > 0 else 0.0
            wide_thr = SPREAD_WIDE_BPS.get(s, SPREAD_WIDE_DEFAULT)
            if spread_bps > wide_thr:
                log.info("REBAL %s skipped — spread %.0f bps > wide threshold %d bps",
                         s, spread_bps, wide_thr)
                continue
            max_notional = MAX_POS_PCT * cap
            target_notional = max(-max_notional, min(max_notional, desired.get(s, 0.0)))
            target_qty = target_notional / mid if mid > 0 else 0.0
            cur_qty = current.get(s, 0.0)
            # Never flip sign in a single rebalance — the exchange's margin check
            # doesn't net, so crossing zero reserves full new-side margin.
            # Reduce to flat first; next rebalance can establish the opposite side.
            if (cur_qty > 0 and target_qty < 0) or (cur_qty < 0 and target_qty > 0):
                target_qty = 0.0
            delta = round(target_qty - cur_qty, 4)
            if abs(delta) * mid < 5.0:  # don't churn tiny
                continue
            exec_px = ba if delta > 0 else bb
            if not _sane_aggressive(exec_px, self.prints[s]):
                log.warning("REBAL %s skipped — %s=%s outside ±%.0f%% of recent prints",
                            s, "ba" if delta > 0 else "bb", exec_px, SANE_MAX_DEV * 100)
                continue
            try:
                if delta > 0:
                    oid = self.client.buy(s, exec_px, delta)
                    if oid:
                        log.info("REBAL LONG %s qty=%s @ %s oid=%s", s, delta, exec_px, oid)
                    else:
                        log.warning("REBAL LONG %s qty=%s @ %s REJECTED (no oid)", s, delta, exec_px)
                else:
                    oid = self.client.sell(s, exec_px, abs(delta))
                    if oid:
                        log.info("REBAL SHORT %s qty=%s @ %s oid=%s", s, abs(delta), exec_px, oid)
                    else:
                        log.warning("REBAL SHORT %s qty=%s @ %s REJECTED (no oid)", s, abs(delta), exec_px)
            except Exception as exc:
                log.warning("rebal %s failed: %s", s, exc)

    def flatten(self):
        try: self.client.cancel_all()
        except Exception: pass

    def run(self):
        last_team = 0.0
        team = {}
        halted = False
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                if state.get("competition_state") != "live":
                    if not halted: self.flatten(); halted = True
                    continue
                halted = False
                self.refresh_symbols(now)
                self.ingest_trades(state)
                if not self.symbols: continue

                if now - last_team > 1.0:
                    try: team = self.client.get_team_state() or {}
                    except Exception as exc: log.warning("get_team_state: %s", exc)
                    last_team = now

                eq = _team_equity(team)
                if eq and eq < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT GUARD: equity %.0f — flattening", eq)
                    self.flatten(); time.sleep(1.0); continue

                cap = _bot_capital(team, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}
                self.rebalance(team, books, cap, now)
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(0.5)


if __name__ == "__main__":
    Engine().run()
