"""
residual_momentum.py — residual momentum after market-beta subtraction (§3.7).

Regress each asset's log-returns on the equal-weighted cross-sectional market
return. The residual series is the idiosyncratic piece. Long assets with high
recent residual momentum, short those with low. Works best with >=5 spots.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("residual_momentum")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

WINDOW = 40
REBAL_TICKS = 15
GROSS_PCT = 0.3
ENTRY_Z = 0.8
BAILOUT_HALT_EQUITY = 150_000
TARGET_TYPES = {"spot","equity","equities"}


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


class ResidualMomentum:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols=[]
        self.prices=defaultdict(lambda: deque(maxlen=WINDOW+5))
        self.tick=0; self.last_rebal=0; self.last_disc=0.0

    def discover(self,now):
        if now-self.last_disc<30 and self.symbols: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.symbols=[_sym(a) for a in assets
                      if _at(a) in TARGET_TYPES and not a.get("halted") and _sym(a)][:15]
        self.last_disc=now

    def rebalance(self, books, ts, cap):
        ready=[s for s in self.symbols if len(self.prices[s])>=WINDOW]
        if len(ready)<5: return
        mat = np.array([list(self.prices[s])[-WINDOW:] for s in ready])
        rets = np.diff(np.log(mat+1e-9), axis=1)  # (N, W-1)
        market = rets.mean(axis=0)  # (W-1,)
        mvar = market.var() or 1e-9
        scores = {}
        for i, s in enumerate(ready):
            beta = np.cov(rets[i], market)[0,1] / mvar
            resid = rets[i] - beta*market
            scores[s] = resid[-10:].sum()  # recent residual momentum
        arr = np.array(list(scores.values()))
        mu, sd = arr.mean(), arr.std() or 1e-9
        weights = {s: (v-mu)/sd for s,v in scores.items() if abs((v-mu)/sd) > ENTRY_Z}
        if not weights: return
        total = sum(abs(v) for v in weights.values()) or 1.0
        for s,w in weights.items(): weights[s] = w/total
        target_gross = GROSS_PCT*cap
        for s,w in weights.items():
            bid,ask = _best(books.get(s) or {})
            if not bid or not ask: continue
            mid = 0.5*(bid+ask)
            target = w * target_gross / mid
            cur = _pos(ts, BOT_ID, s)
            delta = target - cur
            if abs(delta)*mid < 0.002*cap: continue
            try:
                if delta>0: self.client.buy(s, round(ask,4), round(delta,4))
                else: self.client.sell(s, round(bid,4), round(-delta,4))
            except Exception as e: log.warning("%s: %s", s, e)

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
                if now-last_ts>2:
                    try: ts=self.client.get_team_state() or {}
                    except: pass
                    last_ts=now
                if _eq(ts) and _eq(ts)<BAILOUT_HALT_EQUITY:
                    try: self.client.cancel_all()
                    except: pass
                    time.sleep(2); continue
                if self.tick-self.last_rebal>=REBAL_TICKS:
                    self.rebalance(books, ts, _cap(ts,BOT_ID) or 100_000)
                    self.last_rebal=self.tick
            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    ResidualMomentum().run()
