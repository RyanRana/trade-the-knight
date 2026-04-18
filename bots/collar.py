"""
collar.py — zero-cost (ish) collar on large profitable spot positions (§2.53).

For each team spot long bigger than MIN_FRAC of capital, buy an OTM put near
0.95 * S and sell an OTM call near 1.05 * S. Net premium should be near zero.
Protects downside at the cost of capping upside — deploy for end-game defense.
"""

import os, time, logging
from knight_trader import ExchangeClient

log = logging.getLogger("collar")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

PUT_STRIKE_FRAC = 0.95
CALL_STRIKE_FRAC = 1.05
MIN_FRAC = 0.12
MAX_NET_DEBIT_PCT = 0.01
POLL_SEC = 20.0
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

def _all_positions(ts):
    bots = ts.get("bots") or ts.get("portfolio",{}).get("bots") or {}
    it = bots.values() if isinstance(bots, dict) else bots
    totals = {}
    for b in it:
        raw = b.get("positions") or b.get("inventory") or {}
        if isinstance(raw, dict):
            for s, v in raw.items():
                q = _num(v.get("quantity") or v.get("qty") or v.get("size")) if isinstance(v, dict) else _num(v)
                totals[s] = totals.get(s, 0.0) + q
    return totals


class Collar:
    def __init__(self):
        self.client = ExchangeClient()
        self.collared = set()

    def run(self):
        while True:
            try:
                try: ts = self.client.get_team_state() or {}
                except: ts = {}
                if _eq(ts) and _eq(ts) < BAILOUT_HALT_EQUITY:
                    time.sleep(POLL_SEC); continue
                cap = _cap(ts, BOT_ID) or 100_000

                try: assets = self.client.get_assets() or []
                except: time.sleep(POLL_SEC); continue
                if isinstance(assets, dict): assets = list(assets.values())

                puts_by_und = {}; calls_by_und = {}
                for a in assets:
                    if _at(a) not in {"option","options"}: continue
                    cp = (a.get("call_put") or a.get("option_type") or "").lower()
                    und = a.get("underlying") or a.get("underlying_symbol")
                    strike = _num(a.get("strike") or a.get("strike_price"))
                    sym = _sym(a)
                    if not (und and sym and strike): continue
                    (puts_by_und if cp.startswith("p") else calls_by_und).setdefault(und, []).append((strike, sym))

                positions = _all_positions(ts)
                for sym, qty in positions.items():
                    if qty <= 0 or sym in self.collared: continue
                    bid,ask = _best(self.client.get_book(sym) or {})
                    if not bid or not ask: continue
                    S = 0.5*(bid+ask)
                    if qty * S < MIN_FRAC * cap: continue
                    puts = puts_by_und.get(sym); calls = calls_by_und.get(sym)
                    if not puts or not calls: continue
                    pk = PUT_STRIKE_FRAC * S; ck = CALL_STRIKE_FRAC * S
                    puts.sort(key=lambda t: abs(t[0]-pk)); calls.sort(key=lambda t: abs(t[0]-ck))
                    p_k, p_sym = puts[0]; c_k, c_sym = calls[0]
                    pb, pa = _best(self.client.get_book(p_sym) or {}); cb, ca = _best(self.client.get_book(c_sym) or {})
                    if not (pa and cb): continue
                    net_debit = pa - cb
                    if net_debit > MAX_NET_DEBIT_PCT * S: continue
                    try:
                        self.client.buy(p_sym, round(pa,4), round(qty,4))
                        self.client.sell(c_sym, round(cb,4), round(qty,4))
                        self.collared.add(sym)
                        log.info("COLLAR %s qty=%.2f put K=%.2f call K=%.2f net=%.4f", sym, qty, p_k, c_k, net_debit)
                    except Exception as e: log.warning("collar %s: %s", sym, e)
                time.sleep(POLL_SEC)
            except Exception as e:
                log.exception("loop: %s", e); time.sleep(POLL_SEC)


if __name__=="__main__":
    Collar().run()
