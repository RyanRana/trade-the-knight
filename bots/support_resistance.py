"""
support_resistance.py — S/R bounce trader (§3.14).

Tracks rolling local min/max on each asset. Support = average of recent lows,
resistance = average of recent highs. Buys within TOL of support, sells within
TOL of resistance. Hard stop just beyond the level on break.
"""

import os, time, logging
from collections import defaultdict, deque
from knight_trader import ExchangeClient

log = logging.getLogger("support_resistance")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

WINDOW = 50
TOL = 0.005         # 0.5% of price considered "at" a level
STOP_BEYOND = 0.01  # stop 1% past the level on break
POS_PCT = 0.03
MAX_HOLD_TICKS = 40
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


class SupportResistance:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols=[]
        self.hist = defaultdict(lambda: deque(maxlen=WINDOW))
        self.entry_tick = {}
        self.tick=0; self.last_disc=0.0

    def discover(self,now):
        if now-self.last_disc<30 and self.symbols: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.symbols=[_sym(a) for a in assets
                      if _at(a) in TARGET_TYPES and not a.get("halted") and _sym(a)][:15]
        self.last_disc=now

    def _levels(self, arr):
        if len(arr) < WINDOW: return None, None
        a = list(arr)
        lows = sorted(a)[:3]
        highs = sorted(a, reverse=True)[:3]
        return sum(lows)/3.0, sum(highs)/3.0

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
                    self.hist[s].append(mid)
                    sup, res = self._levels(self.hist[s])
                    if sup is None: continue
                    pos = _pos(ts, BOT_ID, s)
                    age = self.tick - self.entry_tick.get(s, self.tick)
                    qty = round((POS_PCT*cap)/mid, 4)

                    # Time stop or level break stop.
                    if pos != 0:
                        break_down = mid < sup * (1 - STOP_BEYOND)
                        break_up = mid > res * (1 + STOP_BEYOND)
                        if age > MAX_HOLD_TICKS or (pos > 0 and break_down) or (pos < 0 and break_up):
                            try:
                                if pos > 0: self.client.sell(s, round(bid,4), round(pos,4))
                                else: self.client.buy(s, round(ask,4), round(-pos,4))
                                self.entry_tick.pop(s,None)
                                log.info("SR EXIT %s age=%d", s, age)
                            except Exception as e: log.warning("exit %s: %s", s, e)
                            continue

                    if pos == 0 and qty > 0:
                        if mid <= sup * (1 + TOL):
                            try:
                                self.client.buy(s, round(ask,4), qty)
                                self.entry_tick[s]=self.tick
                                log.info("SR BUY %s @%.4f sup=%.4f", s, ask, sup)
                            except Exception as e: log.warning("buy %s: %s", s, e)
                        elif mid >= res * (1 - TOL):
                            try:
                                self.client.sell(s, round(bid,4), qty)
                                self.entry_tick[s]=self.tick
                                log.info("SR SELL %s @%.4f res=%.4f", s, bid, res)
                            except Exception as e: log.warning("sell %s: %s", s, e)
            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    SupportResistance().run()
