"""
straddle.py — long straddle ahead of volatility-inducing events (§2.22).

Triggers: underlying's realized vol is unusually low relative to its own
longer-window baseline (compression often precedes expansion), and there's a
nearby ATM call + put available. Buys both. Closes when either leg moves
materially or after MAX_HOLD_TICKS.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("straddle")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

SHORT_W = 15
LONG_W = 60
COMPRESSION_RATIO = 0.4       # short-window vol < 40% of long-window vol → expect expansion
POS_PCT = 0.025
MAX_HOLD_TICKS = 40
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


class Straddle:
    def __init__(self):
        self.client = ExchangeClient()
        self.options=[]
        self.prices = defaultdict(lambda: deque(maxlen=LONG_W+5))
        self.open_until = {}   # underlying -> (call_sym, put_sym, units, expire_tick)
        self.tick=0; self.last_disc=0.0

    def discover(self,now):
        if now-self.last_disc<30 and self.options: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.options=[a for a in assets if _at(a) in {"option","options"} and not a.get("halted")]
        self.last_disc=now

    def _meta(self,a):
        return (_num(a.get("strike") or a.get("strike_price")),
                (a.get("call_put") or a.get("option_type") or "").lower(),
                a.get("underlying") or a.get("underlying_symbol"))

    def run(self):
        ts={}; last_ts=0.0
        for state in self.client.stream_state():
            try:
                now=time.monotonic(); self.tick+=1
                if state.get("competition_state")!="live": continue
                self.discover(now)
                if not self.options: continue
                books = state.get("book") or state.get("books") or {}

                underlyings = {self._meta(o)[2] for o in self.options if self._meta(o)[2]}
                for u in underlyings:
                    bid,ask=_best(books.get(u) or {})
                    if bid and ask: self.prices[u].append(0.5*(bid+ask))

                if now-last_ts>2:
                    try: ts=self.client.get_team_state() or {}
                    except: pass
                    last_ts=now
                if _eq(ts) and _eq(ts)<BAILOUT_HALT_EQUITY:
                    try: self.client.cancel_all()
                    except: pass
                    time.sleep(2); continue
                cap = _cap(ts,BOT_ID) or 100_000

                # Close expired straddles.
                for u, (csym, psym, units, expire_tick) in list(self.open_until.items()):
                    if self.tick >= expire_tick:
                        cb, ca = _best(books.get(csym) or {})
                        pb, pa = _best(books.get(psym) or {})
                        try:
                            if cb: self.client.sell(csym, round(cb,4), units)
                            if pb: self.client.sell(psym, round(pb,4), units)
                            log.info("STRADDLE CLOSE %s", u)
                        except Exception as e: log.warning("close %s: %s", u, e)
                        self.open_until.pop(u, None)

                by_und_calls = defaultdict(list); by_und_puts = defaultdict(list)
                for o in self.options:
                    strike, cp, und = self._meta(o)
                    if not (strike and cp and und): continue
                    lst = by_und_calls[und] if cp.startswith("c") else by_und_puts[und]
                    lst.append((strike, _sym(o)))

                for u in underlyings:
                    if u in self.open_until: continue
                    arr = self.prices.get(u)
                    if not arr or len(arr) < LONG_W: continue
                    a = np.array(arr)
                    r = np.diff(np.log(a+1e-9))
                    short_vol = r[-SHORT_W:].std() or 1e-9
                    long_vol = r.std() or 1e-9
                    if short_vol / long_vol > COMPRESSION_RATIO: continue
                    S = a[-1]
                    calls = by_und_calls.get(u); puts = by_und_puts.get(u)
                    if not calls or not puts: continue
                    calls.sort(key=lambda t: abs(t[0]-S)); puts.sort(key=lambda t: abs(t[0]-S))
                    (_, csym) = calls[0]; (_, psym) = puts[0]
                    cb, ca = _best(books.get(csym) or {}); pb, pa = _best(books.get(psym) or {})
                    if not (ca and pa): continue
                    total_debit = ca + pa
                    units = max(1, int((POS_PCT*cap) / max(total_debit, 0.01)))
                    try:
                        self.client.buy(csym, round(ca,4), units)
                        self.client.buy(psym, round(pa,4), units)
                        self.open_until[u] = (csym, psym, units, self.tick + MAX_HOLD_TICKS)
                        log.info("STRADDLE %s units=%d debit=%.2f", u, units, total_debit)
                    except Exception as e: log.warning("open %s: %s", u, e)

            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    Straddle().run()
