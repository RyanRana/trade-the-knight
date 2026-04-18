"""
bull_call_spread.py — bull call spread (§2.6).

For each underlying with an upward short-term drift, find two calls with
adjacent strikes above the current underlying price. Buy the lower-strike
call, sell the higher-strike call. Defined risk; max loss = net debit.
Entry filter: short-window log-return > threshold. One spread per underlying.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("bull_call_spread")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

DRIFT_WINDOW = 20
DRIFT_THRESH = 0.005          # 0.5% short-term up-drift
MAX_DEBIT_FRAC_OF_WIDTH = 0.4
POS_PCT = 0.03
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


class BullCallSpread:
    def __init__(self):
        self.client = ExchangeClient()
        self.options=[]
        self.prices = defaultdict(lambda: deque(maxlen=DRIFT_WINDOW+5))
        self.opened = set()  # underlying keys where we already placed a spread
        self.last_disc=0.0

    def discover(self,now):
        if now-self.last_disc<30 and self.options: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.options=[a for a in assets if _at(a) in {"option","options"} and not a.get("halted")]
        self.last_disc=now

    def _opt_meta(self,a):
        return (_num(a.get("strike") or a.get("strike_price")),
                (a.get("call_put") or a.get("option_type") or "").lower(),
                a.get("underlying") or a.get("underlying_symbol"))

    def run(self):
        ts={}; last_ts=0.0
        for state in self.client.stream_state():
            try:
                now=time.monotonic()
                if state.get("competition_state")!="live": continue
                self.discover(now)
                if not self.options: continue
                books = state.get("book") or state.get("books") or {}

                # Update underlying drifts.
                underlyings = {self._opt_meta(o)[2] for o in self.options if self._opt_meta(o)[2]}
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

                # Group calls by underlying.
                by_und = defaultdict(list)
                for o in self.options:
                    strike, cp, und = self._opt_meta(o)
                    if cp.startswith("c") and und and strike:
                        by_und[und].append((strike, _sym(o)))

                for und, calls in by_und.items():
                    if und in self.opened: continue
                    arr = self.prices.get(und)
                    if not arr or len(arr) < DRIFT_WINDOW: continue
                    drift = (arr[-1]/arr[0] - 1)
                    if drift < DRIFT_THRESH: continue
                    S = arr[-1]

                    # Pick two adjacent strikes > S.
                    otm = sorted([c for c in calls if c[0] > S], key=lambda x: x[0])
                    if len(otm) < 2: continue
                    (K1, s1), (K2, s2) = otm[0], otm[1]
                    bid1,ask1=_best(books.get(s1) or {}); bid2,ask2=_best(books.get(s2) or {})
                    if not all((bid1,ask1,bid2,ask2)): continue
                    debit = ask1 - bid2
                    width = K2 - K1
                    if width <= 0 or debit > MAX_DEBIT_FRAC_OF_WIDTH * width: continue
                    max_loss_per = debit
                    units = max(1, int((POS_PCT*cap) / max(max_loss_per,0.01)))
                    try:
                        self.client.buy(s1, round(ask1,4), units)
                        self.client.sell(s2, round(bid2,4), units)
                        self.opened.add(und)
                        log.info("BULL CALL %s K1=%.2f K2=%.2f units=%d debit=%.2f", und, K1, K2, units, debit)
                    except Exception as e: log.warning("bull %s: %s", und, e)

            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    BullCallSpread().run()
