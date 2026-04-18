"""
multi_factor.py — composite alpha on spot assets (§3.6).

Combines three signals into a composite score per symbol:
  1. Momentum: short-window z-score of returns
  2. Mean-reversion: -1 * long-window z-score of price vs long MA
  3. Book imbalance: (bid_depth - ask_depth) / (bid_depth + ask_depth)

Long top-third by composite score, short bottom-third. Equal-weighted within
each sleeve. Rebalance every REBAL_TICKS.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("multi_factor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

MOM_WINDOW = 10
REV_WINDOW = 50
REBAL_TICKS = 10
GROSS_PCT = 0.3
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

def _depth(book):
    b=book.get("bids") or {}; a=book.get("asks") or {}
    bd = sum(_num(v) for v in b.values()); ad = sum(_num(v) for v in a.values())
    return bd, ad

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


class MultiFactor:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols=[]
        self.prices = defaultdict(lambda: deque(maxlen=REV_WINDOW+5))
        self.tick=0
        self.last_rebal=0
        self.last_disc=0.0

    def discover(self,now):
        if now-self.last_disc<30 and self.symbols: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.symbols=[_sym(a) for a in assets
                      if _at(a) in TARGET_TYPES and not a.get("halted") and _sym(a)][:20]
        self.last_disc=now

    def scores(self, books):
        out={}
        for s in self.symbols:
            arr = self.prices[s]
            if len(arr) < REV_WINDOW: continue
            a = np.array(arr)
            r = np.diff(np.log(a+1e-9))
            mom = r[-MOM_WINDOW:].mean() / (r[-MOM_WINDOW:].std() or 1e-9)
            rev = -(a[-1] - a.mean()) / (a.std() or 1e-9)
            bd, ad = _depth(books.get(s) or {})
            imb = (bd-ad)/(bd+ad+1e-9)
            out[s] = mom + rev + imb
        return out

    def rebalance(self, books, ts, cap):
        sc = self.scores(books)
        if len(sc) < 6: return
        items = sorted(sc.items(), key=lambda x: x[1])
        third = max(1, len(items)//3)
        shorts = [s for s,_ in items[:third]]
        longs = [s for s,_ in items[-third:]]
        per = GROSS_PCT * cap / (2 * max(1, third))
        for s in longs + shorts:
            bid, ask = _best(books.get(s) or {})
            if not bid or not ask: continue
            mid = 0.5*(bid+ask)
            target_qty = (per/mid) * (1 if s in longs else -1)
            cur = _pos(ts, BOT_ID, s)
            delta = target_qty - cur
            if abs(delta)*mid < 0.002*cap: continue
            try:
                if delta > 0: self.client.buy(s, round(ask,4), round(delta,4))
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
    MultiFactor().run()
