"""
ma_crossover.py — classic fast/slow moving-average crossover (§3.12).

Fast EMA(5) vs slow EMA(20). Golden cross → long, death cross → short. Adds a
minimum separation filter to suppress whipsaws in chop.
"""

import os, time, logging
from knight_trader import ExchangeClient

log = logging.getLogger("ma_crossover")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

ALPHA_FAST = 2.0 / (5 + 1)
ALPHA_SLOW = 2.0 / (20 + 1)
MIN_SEP_PCT = 0.01   # fast must be at least 1% away from slow
POS_PCT = 0.03
MAX_HOLD_TICKS = 50
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


class MACrossover:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols=[]
        self.fast={}; self.slow={}; self.prev_state={}
        self.hold_age={}
        self.tick=0; self.last_disc=0.0

    def discover(self,now):
        if now-self.last_disc<30 and self.symbols: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.symbols=[_sym(a) for a in assets
                      if _at(a) in TARGET_TYPES and not a.get("halted") and _sym(a)][:15]
        self.last_disc=now

    def run(self):
        ts={}; last_ts=0.0
        for state in self.client.stream_state():
            try:
                now=time.monotonic(); self.tick+=1
                if state.get("competition_state")!="live": continue
                self.discover(now)
                if now-last_ts>2:
                    try: ts=self.client.get_team_state() or {}
                    except: pass
                    last_ts=now
                if _eq(ts) and _eq(ts)<BAILOUT_HALT_EQUITY:
                    try: self.client.cancel_all()
                    except: pass
                    time.sleep(2); continue
                cap = _cap(ts,BOT_ID) or 100_000
                books = state.get("book") or state.get("books") or {}
                for s in self.symbols:
                    bid,ask=_best(books.get(s) or {})
                    if not bid or not ask: continue
                    mid=0.5*(bid+ask)
                    f = self.fast.get(s, mid); sl = self.slow.get(s, mid)
                    f = f + ALPHA_FAST*(mid-f); sl = sl + ALPHA_SLOW*(mid-sl)
                    self.fast[s]=f; self.slow[s]=sl
                    sep = (f - sl) / (sl or 1e-9)
                    prev = self.prev_state.get(s, 0)
                    now_sig = 1 if sep > MIN_SEP_PCT else (-1 if sep < -MIN_SEP_PCT else 0)
                    pos = _pos(ts, BOT_ID, s)
                    age = self.tick - self.hold_age.get(s, self.tick)
                    qty = round((POS_PCT*cap)/mid, 4)
                    if qty <= 0: continue

                    # Time stop.
                    if pos != 0 and age > MAX_HOLD_TICKS:
                        try:
                            if pos>0: self.client.sell(s, round(bid,4), round(pos,4))
                            else: self.client.buy(s, round(ask,4), round(-pos,4))
                            self.hold_age.pop(s,None)
                        except Exception as e: log.warning("%s stop: %s", s, e)

                    # Cross events.
                    if now_sig == 1 and prev != 1 and pos <= 0:
                        try:
                            if pos < 0: self.client.buy(s, round(ask,4), round(-pos,4))
                            self.client.buy(s, round(ask,4), qty)
                            self.hold_age[s] = self.tick
                            log.info("GOLDEN %s @%.4f", s, ask)
                        except Exception as e: log.warning("%s golden: %s", s, e)
                    elif now_sig == -1 and prev != -1 and pos >= 0:
                        try:
                            if pos > 0: self.client.sell(s, round(bid,4), round(pos,4))
                            self.client.sell(s, round(bid,4), qty)
                            self.hold_age[s] = self.tick
                            log.info("DEATH %s @%.4f", s, bid)
                        except Exception as e: log.warning("%s death: %s", s, e)

                    self.prev_state[s] = now_sig
            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    MACrossover().run()
