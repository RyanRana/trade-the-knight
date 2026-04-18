"""
momentum_single.py — simple single-asset price momentum (§3.1).

For each asset, 10-tick return > THRESH → long; < -THRESH → short. Hard stop
at -STOP_LOSS from entry. Take profit at +TAKE_PROFIT. Time stop at
MAX_HOLD_TICKS. Signal reversal flips.
"""

import os, time, logging
from collections import defaultdict, deque
from knight_trader import ExchangeClient

log = logging.getLogger("momentum_single")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

RET_WINDOW = 10
THRESH = 0.015
STOP_LOSS = 0.02
TAKE_PROFIT = 0.03
MAX_HOLD_TICKS = 20
POS_PCT = 0.04
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


class MomentumSingle:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols=[]
        self.prices=defaultdict(lambda: deque(maxlen=RET_WINDOW+5))
        self.entry_px={}; self.entry_tick={}
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
                    self.prices[s].append(mid)
                    if len(self.prices[s]) < RET_WINDOW: continue
                    r = self.prices[s][-1] / self.prices[s][0] - 1
                    pos = _pos(ts, BOT_ID, s)
                    qty = round((POS_PCT*cap)/mid, 4)
                    if qty <= 0: continue

                    # Exit logic.
                    if pos != 0:
                        entry = self.entry_px.get(s, mid)
                        pnl = (mid/entry - 1) * (1 if pos>0 else -1)
                        age = self.tick - self.entry_tick.get(s, self.tick)
                        if pnl <= -STOP_LOSS or pnl >= TAKE_PROFIT or age > MAX_HOLD_TICKS:
                            try:
                                if pos>0: self.client.sell(s, round(bid,4), round(pos,4))
                                else: self.client.buy(s, round(ask,4), round(-pos,4))
                                log.info("EXIT %s pnl=%.3f age=%d", s, pnl, age)
                                self.entry_px.pop(s, None); self.entry_tick.pop(s,None)
                            except Exception as e: log.warning("exit %s: %s", s, e)
                            continue
                        # Flip on reversal.
                        if (pos>0 and r < -THRESH) or (pos<0 and r > THRESH):
                            try:
                                if pos>0: self.client.sell(s, round(bid,4), round(pos,4))
                                else: self.client.buy(s, round(ask,4), round(-pos,4))
                                self.entry_px.pop(s, None); self.entry_tick.pop(s,None)
                            except Exception as e: log.warning("flip %s: %s", s, e)
                            continue

                    # Entry.
                    if pos == 0:
                        if r > THRESH:
                            try:
                                self.client.buy(s, round(ask,4), qty)
                                self.entry_px[s]=ask; self.entry_tick[s]=self.tick
                                log.info("LONG %s r=%.3f", s, r)
                            except Exception as e: log.warning("long %s: %s", s, e)
                        elif r < -THRESH:
                            try:
                                self.client.sell(s, round(bid,4), qty)
                                self.entry_px[s]=bid; self.entry_tick[s]=self.tick
                                log.info("SHORT %s r=%.3f", s, r)
                            except Exception as e: log.warning("short %s: %s", s, e)

            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    MomentumSingle().run()
