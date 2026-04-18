"""
bayes_pred.py — naïve-Bayes-style probability updates on prediction markets (§18.3).

For each prediction market, track correlated public timeseries features. On
each tick, bin each feature's recent value into quantiles and update a Bayesian
posterior. When posterior diverges from market price by > THRESHOLD, trade.

This is a lightweight online learner — no offline training. We use a simple
empirical Bayes approach: maintain per-feature counts of (feature bin, market
direction) and estimate P(YES | feature bins) as the smoothed product.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("bayes_pred")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

FEATURE_HISTORY = 120
BINS = 3
EDGE = 0.12
POS_PCT = 0.05
BAILOUT_HALT_EQUITY = 150_000
PRED_TYPES = {"prediction","prediction_market","yes_share","pm"}


def _num(x,d=0.0):
    try: return float(x)
    except: return d
def _at(a): return (a.get("asset_type") or a.get("type") or "").lower()
def _sym(a): return a.get("symbol") or a.get("id") or a.get("name")

def _best(book):
    b=book.get("bids") or {}; a=book.get("asks") or {}
    bp=[_num(k) for k in b if _num(k)>0]; ap=[_num(k) for k in a if _num(k)>0]
    return (max(bp) if bp else None, min(ap) if ap else None)

def _eq(ts):
    for k in ("total_equity","equity","leaderboard_equity"):
        if k in ts: return _num(ts[k])
    return 0.0

def _cap(ts,bid):
    bs = ts.get("bots") or ts.get("portfolio",{}).get("bots") or {}
    b = bs.get(bid,{}) if isinstance(bs,dict) else next((x for x in bs if x.get("id")==bid),{})
    return _num(b.get("capital") or b.get("allocated_capital"))

def _pos(ts,bid,sym):
    bs = ts.get("bots") or ts.get("portfolio",{}).get("bots") or {}
    b = bs.get(bid,{}) if isinstance(bs,dict) else next((x for x in bs if x.get("id")==bid),{})
    raw = b.get("positions") or b.get("inventory") or {}
    if isinstance(raw,dict):
        v = raw.get(sym) or {}
        return _num(v.get("quantity") or v.get("qty") or v.get("size")) if isinstance(v,dict) else _num(v)
    return 0.0


class BayesPred:
    def __init__(self):
        self.client = ExchangeClient()
        self.markets = []
        self.feature_series = []
        self.feature_hist = defaultdict(lambda: deque(maxlen=FEATURE_HISTORY))
        self.market_hist = defaultdict(lambda: deque(maxlen=FEATURE_HISTORY))  # mid-price as proxy outcome signal
        self.last_disc = 0.0

    def discover(self, now):
        if now-self.last_disc<60 and self.markets: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.markets = [_sym(a) for a in assets if _at(a) in PRED_TYPES and _sym(a)]
        try: series = self.client.list_timeseries() or []
        except: series = []
        self.feature_series = [s.get("name") if isinstance(s,dict) else str(s) for s in series]
        self.last_disc = now

    def _bin(self, arr, v):
        if len(arr) < BINS*4: return None
        qs = np.quantile(arr, [i/BINS for i in range(1, BINS)])
        for i, q in enumerate(qs):
            if v <= q: return i
        return BINS - 1

    def _posterior(self, market):
        # Use current feature bins and the market's own recent history as weak labels.
        mh = self.market_hist.get(market)
        if not mh or len(mh) < 20: return None
        # "YES-like" label: market rose in last N ticks → treat as tending YES.
        labels = np.diff(np.array(mh)) > 0
        if labels.sum() < 3 or (~labels).sum() < 3: return None
        prior = labels.mean()
        log_odds = np.log(prior/(1-prior+1e-9) + 1e-9)
        for feat in self.feature_series[:6]:  # cap features for CPU budget
            hist = list(self.feature_hist[feat])
            if len(hist) < 30: continue
            current = hist[-1]
            b = self._bin(hist, current)
            if b is None: continue
            # Align feature history to label indices (both length-aligned by time).
            feat_arr = np.array(hist[-len(labels):-0] if len(hist) > len(labels) else hist[-len(labels):])
            if len(feat_arr) != len(labels): continue
            bins_arr = np.array([self._bin(hist, v) for v in feat_arr])
            mask = bins_arr == b
            if mask.sum() < 3: continue
            p_b_given_y = max(0.01, min(0.99, labels[mask].mean()))
            p_b_given_n = max(0.01, min(0.99, (~labels)[mask].mean() if mask.sum()>0 else 0.5))
            log_odds += np.log((p_b_given_y+1e-9)/(p_b_given_n+1e-9))
        return 1/(1+np.exp(-log_odds))

    def run(self):
        ts={}; last_ts=0.0; last_fetch=0.0
        for state in self.client.stream_state():
            try:
                now=time.monotonic()
                if state.get("competition_state")!="live": continue
                self.discover(now)
                if not self.markets: continue

                # Update feature timeseries snapshots.
                if now-last_fetch>5:
                    for f in self.feature_series[:6]:
                        try:
                            pts = self.client.get_timeseries(f, limit=1) or []
                            if pts:
                                last = pts[-1]
                                v = last.get("v") if isinstance(last,dict) else last
                                self.feature_hist[f].append(_num(v))
                        except: pass
                    last_fetch = now

                books = state.get("book") or state.get("books") or {}
                for m in self.markets:
                    bid, ask = _best(books.get(m) or {})
                    if bid and ask:
                        self.market_hist[m].append(0.5*(bid+ask))

                if now-last_ts>2:
                    try: ts=self.client.get_team_state() or {}
                    except: pass
                    last_ts=now
                if _eq(ts) and _eq(ts)<BAILOUT_HALT_EQUITY:
                    try: self.client.cancel_all()
                    except: pass
                    time.sleep(2); continue
                cap = _cap(ts,BOT_ID) or 100_000

                for m in self.markets:
                    p = self._posterior(m)
                    if p is None: continue
                    bid, ask = _best(books.get(m) or {})
                    if not bid or not ask: continue
                    pos = _pos(ts, BOT_ID, m)
                    qty = max(1, round((POS_PCT*cap)/max(ask,0.02),2))
                    if p - ask > EDGE and pos <= 0:
                        try:
                            self.client.buy(m, round(ask,2), qty)
                            log.info("BAYES BUY %s @%.2f p=%.2f", m, ask, p)
                        except Exception as e: log.warning("%s: %s", m, e)
                    elif bid - p > EDGE and pos >= 0:
                        try:
                            self.client.sell(m, round(bid,2), qty)
                            log.info("BAYES SELL %s @%.2f p=%.2f", m, bid, p)
                        except Exception as e: log.warning("%s: %s", m, e)

            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    BayesPred().run()
