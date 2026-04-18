"""
fx_carry.py — FX carry trade (§8.2).

On this exchange IOR pays on RUD; FX pairs don't have explicit rates, but
we proxy "carry" via sustained drift. For each pair, compute long-window
return + compare to ior_rate. Long pairs drifting above ior_rate, short those
drifting below. Keeps total exposure under CAP_PCT.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("fx_carry")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

WINDOW = 80
REBAL_TICKS = 20
CAP_PCT = 0.2
BAILOUT_HALT_EQUITY = 150_000


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

def _ior(client):
    for n in ("ior_rate","fed_rate"):
        try:
            pts = client.get_timeseries(n, limit=1) or []
            if pts:
                v = pts[-1].get("v") if isinstance(pts[-1],dict) else pts[-1]
                if _num(v)>0: return _num(v)
        except: continue
    return 0.05


class FXCarry:
    def __init__(self):
        self.client = ExchangeClient()
        self.pairs=[]
        self.prices=defaultdict(lambda: deque(maxlen=WINDOW+5))
        self.tick=0; self.last_rebal=0; self.last_disc=0.0

    def discover(self,now):
        if now-self.last_disc<30 and self.pairs: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.pairs=[_sym(a) for a in assets if _at(a) in {"forex","fx"} and not a.get("halted") and _sym(a)]
        self.last_disc=now

    def rebalance(self, books, ts, cap):
        ready=[p for p in self.pairs if len(self.prices[p])>=WINDOW]
        if not ready: return
        ior = _ior(self.client)
        scores = {}
        for p in ready:
            arr=np.array(self.prices[p])
            annualized = (np.log(arr[-1]/arr[0])) * (252.0 / WINDOW)  # rough annualized drift
            scores[p] = annualized - ior
        total = sum(abs(s) for s in scores.values()) or 1.0
        for p, s in scores.items():
            if abs(s) < 0.01: continue  # 1% hurdle
            target_notional = (s/total) * CAP_PCT * cap
            bid,ask = _best(books.get(p) or {})
            if not bid or not ask: continue
            mid=0.5*(bid+ask)
            target = target_notional / mid
            cur = _pos(ts, BOT_ID, p)
            delta = target - cur
            if abs(delta)*mid < 0.002*cap: continue
            try:
                if delta>0: self.client.buy(p, round(ask,6), round(delta,4))
                else: self.client.sell(p, round(bid,6), round(-delta,4))
            except Exception as e: log.warning("%s: %s", p, e)

    def run(self):
        ts={}; last_ts=0.0
        for state in self.client.stream_state():
            try:
                now=time.monotonic(); self.tick+=1
                if state.get("competition_state")!="live": continue
                self.discover(now)
                books = state.get("book") or state.get("books") or {}
                for p in self.pairs:
                    bid,ask=_best(books.get(p) or {})
                    if bid and ask: self.prices[p].append(0.5*(bid+ask))
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
    FXCarry().run()
