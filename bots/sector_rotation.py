"""
sector_rotation.py — momentum rotation across spot/FX (§4.1).

Every ROT_TICKS, rank assets by ROT_WINDOW return. Allocate to top performer,
partially to runner-up, zero to the rest. Only rotate when the top rank
actually changes (hysteresis to avoid churn).
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("sector_rotation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

ROT_WINDOW = 20
ROT_TICKS = 15
ALLOC_PCT = 0.3
BAILOUT_HALT_EQUITY = 150_000
TARGET_TYPES = {"spot","equity","equities","forex","fx"}


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


class SectorRotation:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols=[]
        self.prices = defaultdict(lambda: deque(maxlen=ROT_WINDOW+5))
        self.last_rank=()
        self.tick=0; self.last_rot=0; self.last_disc=0.0

    def discover(self,now):
        if now-self.last_disc<30 and self.symbols: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.symbols=[_sym(a) for a in assets
                      if _at(a) in TARGET_TYPES and not a.get("halted") and _sym(a)][:15]
        self.last_disc=now

    def rotate(self, books, ts, cap):
        ready=[s for s in self.symbols if len(self.prices[s])>=ROT_WINDOW]
        if len(ready) < 3: return
        rets = {s: self.prices[s][-1]/self.prices[s][0] - 1 for s in ready}
        ranked = sorted(ready, key=lambda s: rets[s], reverse=True)
        top, second = ranked[0], ranked[1]
        new_rank = (top, second)
        if new_rank == self.last_rank: return
        self.last_rank = new_rank

        weights = {s: 0.0 for s in ready}
        weights[top] = 0.7
        weights[second] = 0.3

        for s in ready:
            bid,ask=_best(books.get(s) or {})
            if not bid or not ask: continue
            mid=0.5*(bid+ask)
            target_qty = (weights[s] * ALLOC_PCT * cap) / mid
            cur = _pos(ts, BOT_ID, s)
            delta = target_qty - cur
            if abs(delta)*mid < 0.002*cap: continue
            try:
                if delta>0: self.client.buy(s, round(ask,4), round(delta,4))
                else: self.client.sell(s, round(bid,4), round(-delta,4))
            except Exception as e: log.warning("%s: %s", s, e)
        log.info("rotated top=%s second=%s", top, second)

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
                if self.tick-self.last_rot>=ROT_TICKS:
                    self.rotate(books, ts, _cap(ts,BOT_ID) or 100_000)
                    self.last_rot=self.tick
            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    SectorRotation().run()
