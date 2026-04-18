"""
iron_condor.py — iron condor on range-bound underlyings (§2.50).

For each underlying that's been range-bound (recent std / mean < RANGE_FRAC):
- Sell OTM call at K3, buy further-OTM call at K4 (K4 > K3)
- Sell OTM put at K2, buy further-OTM put at K1 (K1 < K2)
Net credit. Max loss = max(K4-K3, K2-K1) - net_credit. Close at 50% of max
profit or if underlying breaches the short strikes.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("iron_condor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

WINDOW = 50
RANGE_FRAC = 0.01      # price std/mean must be under 1% to count as range-bound
OTM_FRAC_SHORT = 0.03  # short strikes ~3% OTM
OTM_FRAC_LONG = 0.06   # long protection strikes ~6% OTM
POS_PCT = 0.03
CLOSE_PROFIT_PCT = 0.5
MAX_HOLD_TICKS = 60
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


class IronCondor:
    def __init__(self):
        self.client = ExchangeClient()
        self.options=[]
        self.prices = defaultdict(lambda: deque(maxlen=WINDOW+5))
        self.open = {}   # und -> (short_call_sym, long_call_sym, short_put_sym, long_put_sym, units, credit, open_tick, K_short_call, K_short_put)
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

                # Close expired/breached condors.
                for u, pack in list(self.open.items()):
                    sc, lc, sp, lp, units, credit, open_tick, K_sc, K_sp = pack
                    age = self.tick - open_tick
                    bid, ask = _best(books.get(u) or {})
                    S = 0.5*(bid+ask) if (bid and ask) else None
                    breached = S and (S > K_sc or S < K_sp)
                    if age > MAX_HOLD_TICKS or breached:
                        try:
                            # Close legs: buy back the shorts, sell the longs.
                            for sym, side in ((sc,"buy"),(sp,"buy"),(lc,"sell"),(lp,"sell")):
                                b,a = _best(books.get(sym) or {})
                                px = a if side=="buy" else b
                                if px is None: continue
                                if side=="buy": self.client.buy(sym, round(px,4), units)
                                else: self.client.sell(sym, round(px,4), units)
                            log.info("CONDOR CLOSE %s age=%d breached=%s", u, age, breached)
                        except Exception as e: log.warning("close %s: %s", u, e)
                        self.open.pop(u, None)

                calls_by = defaultdict(list); puts_by = defaultdict(list)
                for o in self.options:
                    strike, cp, und = self._meta(o)
                    if not (strike and cp and und): continue
                    (calls_by if cp.startswith("c") else puts_by)[und].append((strike, _sym(o)))

                for u in underlyings:
                    if u in self.open: continue
                    arr = self.prices.get(u)
                    if not arr or len(arr) < WINDOW: continue
                    a = np.array(arr)
                    if a.std() / (a.mean() or 1e-9) > RANGE_FRAC: continue
                    S = a[-1]
                    calls = calls_by.get(u); puts = puts_by.get(u)
                    if not calls or not puts: continue
                    # Short strikes ~3% OTM, long strikes ~6% OTM.
                    K_sc = S * (1 + OTM_FRAC_SHORT); K_lc = S * (1 + OTM_FRAC_LONG)
                    K_sp = S * (1 - OTM_FRAC_SHORT); K_lp = S * (1 - OTM_FRAC_LONG)
                    short_call = min(calls, key=lambda t: abs(t[0]-K_sc))
                    long_call  = min(calls, key=lambda t: abs(t[0]-K_lc))
                    short_put  = min(puts,  key=lambda t: abs(t[0]-K_sp))
                    long_put   = min(puts,  key=lambda t: abs(t[0]-K_lp))
                    if short_call[0] >= long_call[0] or short_put[0] <= long_put[0]: continue
                    sc_sym = short_call[1]; lc_sym = long_call[1]; sp_sym = short_put[1]; lp_sym = long_put[1]
                    sc_b,_ = _best(books.get(sc_sym) or {}); _,lc_a = _best(books.get(lc_sym) or {})
                    sp_b,_ = _best(books.get(sp_sym) or {}); _,lp_a = _best(books.get(lp_sym) or {})
                    if not (sc_b and lc_a and sp_b and lp_a): continue
                    credit = (sc_b - lc_a) + (sp_b - lp_a)
                    width = max(long_call[0]-short_call[0], short_put[0]-long_put[0])
                    if credit <= 0 or width <= 0: continue
                    max_loss_per = width - credit
                    if max_loss_per <= 0: continue
                    units = max(1, int((POS_PCT*cap) / max_loss_per))
                    try:
                        self.client.sell(sc_sym, round(sc_b,4), units)
                        self.client.buy(lc_sym, round(lc_a,4), units)
                        self.client.sell(sp_sym, round(sp_b,4), units)
                        self.client.buy(lp_sym, round(lp_a,4), units)
                        self.open[u] = (sc_sym, lc_sym, sp_sym, lp_sym, units, credit, self.tick,
                                        short_call[0], short_put[0])
                        log.info("CONDOR %s units=%d credit=%.2f width=%.2f", u, units, credit, width)
                    except Exception as e: log.warning("open %s: %s", u, e)

            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    IronCondor().run()
