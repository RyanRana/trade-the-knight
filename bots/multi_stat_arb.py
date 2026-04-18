"""
multi_stat_arb.py — dollar-neutral multi-asset statistical arbitrage (§3.18).

Every REBAL_TICKS, compute returns for all tracked spots, z-score them,
allocate long to low-z names, short to high-z names, weighted by inverse
variance. Net dollar-neutral. Targets are rebalanced to desired weights.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("multi_stat_arb")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

RET_WINDOW = 20
REBAL_TICKS = 10
ENTRY_Z = 1.0
GROSS_PCT = 0.4  # gross notional cap = 40% of capital
BAILOUT_HALT_EQUITY = 150_000
TARGET_TYPES = {"spot","equity","equities"}


def _num(x,d=0.0):
    try: return float(x)
    except: return d

def _asset_type(a): return (a.get("asset_type") or a.get("type") or "").lower()
def _symbol_of(a): return a.get("symbol") or a.get("id") or a.get("name")

def _best(book):
    b = book.get("bids") or {}; a = book.get("asks") or {}
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


class MultiStatArb:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols = []
        self.prices = defaultdict(lambda: deque(maxlen=RET_WINDOW+5))
        self.last_rebal = 0
        self.last_discovery = 0.0
        self.tick = 0

    def discover(self, now):
        if now - self.last_discovery < 30 and self.symbols: return
        try: assets = self.client.get_assets() or []
        except: return
        if isinstance(assets, dict): assets = list(assets.values())
        self.symbols = [_symbol_of(a) for a in assets
                        if _asset_type(a) in TARGET_TYPES and not a.get("halted") and _symbol_of(a)][:25]
        self.last_discovery = now

    def rebalance(self, books, ts, cap):
        ready = [s for s in self.symbols if len(self.prices[s]) >= RET_WINDOW]
        if len(ready) < 4: return
        rets = {}
        vars_ = {}
        for s in ready:
            arr = np.array(self.prices[s])
            r = np.diff(np.log(arr + 1e-9))
            rets[s] = r[-1] if len(r) else 0.0
            vars_[s] = (r.var() or 1e-6)
        r_vals = np.array(list(rets.values()))
        mu, sd = r_vals.mean(), (r_vals.std() or 1e-9)
        zs = {s: (rets[s]-mu)/sd for s in ready}

        # Weights: inverse-variance, sign flipped (short winners, long losers).
        raw = {s: -zs[s] / vars_[s] for s in ready if abs(zs[s]) > ENTRY_Z}
        if not raw: return
        total = sum(abs(v) for v in raw.values()) or 1.0
        # Dollar-neutral: normalize to ±1, then scale by GROSS_PCT * cap.
        weights = {s: v/total for s,v in raw.items()}
        long_sum = sum(w for w in weights.values() if w>0)
        short_sum = -sum(w for w in weights.values() if w<0)
        if long_sum>0 and short_sum>0:
            for s in weights:
                if weights[s]>0: weights[s] *= 0.5/long_sum
                else: weights[s] *= 0.5/short_sum

        target_gross = GROSS_PCT * cap
        for s, w in weights.items():
            target_notional = w * target_gross
            bid, ask = _best(books.get(s) or {})
            if not bid or not ask: continue
            mid = 0.5*(bid+ask)
            target_qty = target_notional / mid
            current = _pos(ts, BOT_ID, s)
            delta = target_qty - current
            if abs(delta) * mid < 0.002 * cap: continue  # threshold
            try:
                if delta > 0: self.client.buy(s, round(ask,4), round(delta,4))
                else: self.client.sell(s, round(bid,4), round(-delta,4))
            except Exception as e: log.warning("trade %s: %s", s, e)
        log.info("rebalanced %d names", len(weights))

    def run(self):
        ts={}; last_ts=0.0
        for state in self.client.stream_state():
            try:
                now=time.monotonic(); self.tick+=1
                if state.get("competition_state")!="live": continue
                self.discover(now)
                books = state.get("book") or state.get("books") or {}
                for s in self.symbols:
                    bid,ask=_best(books.get(s) or {})
                    if bid and ask: self.prices[s].append(0.5*(bid+ask))
                if now-last_ts > 2:
                    try: ts = self.client.get_team_state() or {}
                    except: pass
                    last_ts = now
                if _eq(ts) and _eq(ts) < BAILOUT_HALT_EQUITY:
                    try: self.client.cancel_all()
                    except: pass
                    time.sleep(2); continue
                if self.tick - self.last_rebal >= REBAL_TICKS:
                    self.rebalance(books, ts, _cap(ts, BOT_ID) or 100_000)
                    self.last_rebal = self.tick
            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__ == "__main__":
    MultiStatArb().run()
