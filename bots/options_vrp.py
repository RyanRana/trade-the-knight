"""
options_vrp.py — volatility risk premium (§7.4).

Identifies options whose implied vol (back-solved from market price) materially
exceeds realized vol of the underlying. Sells such options and delta-hedges
with the underlying spot. Closes near expiry or when implied/realized converges.

Approximate IV: uses intrinsic + time-value model. Since a cash-settled option
near expiry is approximately max(intrinsic, 0), we avoid exotic models and
compare premium above intrinsic to recent underlying move.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("options_vrp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

RV_WINDOW = 30
MIN_VRP_BPS = 300    # sell when IV surplus >= 3% of underlying price
MAX_UNITS_PER_OPTION = 5
POS_PCT = 0.04
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


class OptionsVRP:
    def __init__(self):
        self.client = ExchangeClient()
        self.options = []  # list of option asset dicts
        self.underlying_prices = defaultdict(lambda: deque(maxlen=RV_WINDOW+5))
        self.last_disc=0.0

    def discover(self,now):
        if now-self.last_disc<30 and self.options: return
        try: assets=self.client.get_assets() or []
        except: return
        if isinstance(assets,dict): assets=list(assets.values())
        self.options = [a for a in assets if _at(a) in {"option","options"} and not a.get("halted")]
        self.last_disc = now

    def _opt_meta(self, a):
        strike = _num(a.get("strike") or a.get("strike_price"))
        cp = (a.get("call_put") or a.get("type_cp") or a.get("option_type") or "").lower()
        und = a.get("underlying") or a.get("underlying_symbol") or a.get("underlying_asset")
        return strike, cp, und

    def _intrinsic(self, cp, strike, S):
        if cp.startswith("c"): return max(0.0, S - strike)
        return max(0.0, strike - S)

    def _delta(self, cp, strike, S):
        # crude: 1 if ITM call, 0 if OTM call; -1 ITM put, 0 OTM put.
        if cp.startswith("c"): return 1.0 if S > strike else 0.0
        return -1.0 if S < strike else 0.0

    def run(self):
        ts={}; last_ts=0.0
        for state in self.client.stream_state():
            try:
                now=time.monotonic()
                if state.get("competition_state")!="live": continue
                self.discover(now)
                if not self.options: continue
                books = state.get("book") or state.get("books") or {}

                # Update underlying price history for realized vol.
                underlyings = {self._opt_meta(o)[2] for o in self.options if self._opt_meta(o)[2]}
                for u in underlyings:
                    bid, ask = _best(books.get(u) or {})
                    if bid and ask: self.underlying_prices[u].append(0.5*(bid+ask))

                if now-last_ts>2:
                    try: ts=self.client.get_team_state() or {}
                    except: pass
                    last_ts=now
                if _eq(ts) and _eq(ts)<BAILOUT_HALT_EQUITY:
                    try: self.client.cancel_all()
                    except: pass
                    time.sleep(2); continue
                cap = _cap(ts,BOT_ID) or 100_000

                for opt in self.options:
                    sym = _sym(opt)
                    strike, cp, und = self._opt_meta(opt)
                    if not (sym and strike and cp and und): continue
                    ub, ua = _best(books.get(und) or {})
                    if not ub or not ua: continue
                    S = 0.5*(ub+ua)
                    arr = self.underlying_prices.get(und)
                    if not arr or len(arr) < RV_WINDOW: continue
                    realized_move = np.std(np.diff(np.log(np.array(arr)+1e-9))) * S * np.sqrt(RV_WINDOW)

                    ob, oa = _best(books.get(sym) or {})
                    if not ob or not oa: continue
                    intrinsic = self._intrinsic(cp, strike, S)
                    time_value = max(0.0, ob - intrinsic)  # what a seller collects above intrinsic
                    edge = time_value - realized_move
                    edge_bps = 1e4 * edge / max(S, 1e-6)

                    pos = _pos(ts, BOT_ID, sym)

                    if edge_bps >= MIN_VRP_BPS and pos >= 0:
                        qty = min(MAX_UNITS_PER_OPTION, max(1, int((POS_PCT*cap)/max(ob,0.01))))
                        try:
                            self.client.sell(sym, round(ob,4), qty)
                            # Delta hedge.
                            delta = self._delta(cp, strike, S)
                            if delta != 0:
                                hedge_qty = abs(delta) * qty
                                if delta > 0:  # short call → long underlying to hedge
                                    self.client.buy(und, round(ua,4), round(hedge_qty,4))
                                else:  # short put → short underlying
                                    self.client.sell(und, round(ub,4), round(hedge_qty,4))
                            log.info("VRP SELL %s qty=%d edge=%.0fbps", sym, qty, edge_bps)
                        except Exception as e: log.warning("vrp %s: %s", sym, e)

                    # Unwind when edge collapses or we're offered to buy back cheap.
                    if pos < 0 and edge_bps < MIN_VRP_BPS/3:
                        try:
                            self.client.buy(sym, round(oa,4), int(-pos))
                            log.info("VRP CLOSE %s pos=%d", sym, int(pos))
                        except Exception as e: log.warning("close %s: %s", sym, e)

            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__=="__main__":
    OptionsVRP().run()
