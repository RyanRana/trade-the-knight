"""
protective_put.py — buy OTM puts on large spot positions (§2.4).

Watches team spot inventory. For any position larger than MIN_PROTECT_NOTIONAL
and not already hedged, finds the nearest OTM put (strike ~ 0.9 * S) on that
underlying and buys enough puts to cover the position. Skips if premium exceeds
MAX_PREMIUM_PCT of the position's notional.
"""

import os, time, logging
from knight_trader import ExchangeClient

log = logging.getLogger("protective_put")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

PROTECT_STRIKE_FRAC = 0.9
MIN_PROTECT_FRAC = 0.1       # hedge positions worth > 10% of capital
MAX_PREMIUM_PCT = 0.05       # premium up to 5% of position notional
POLL_SEC = 15.0
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
    """Aggregate positions across all bots on our team (team-level hedging view)."""
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


class ProtectivePut:
    def __init__(self):
        self.client = ExchangeClient()
        self.hedged = set()  # (underlying) already hedged keys

    def _assets(self):
        try: a = self.client.get_assets() or []
        except: return []
        return list(a.values()) if isinstance(a, dict) else a

    def run(self):
        while True:
            try:
                try: ts = self.client.get_team_state() or {}
                except: ts = {}
                if _eq(ts) and _eq(ts) < BAILOUT_HALT_EQUITY:
                    time.sleep(POLL_SEC); continue
                cap = _cap(ts, BOT_ID) or 100_000
                min_notional = MIN_PROTECT_FRAC * cap

                assets = self._assets()
                puts_by_und = {}
                for a in assets:
                    if _at(a) not in {"option","options"}: continue
                    cp = (a.get("call_put") or a.get("option_type") or "").lower()
                    if not cp.startswith("p"): continue
                    und = a.get("underlying") or a.get("underlying_symbol")
                    strike = _num(a.get("strike") or a.get("strike_price"))
                    sym = _sym(a)
                    if und and sym and strike:
                        puts_by_und.setdefault(und, []).append((strike, sym))

                positions = _all_positions(ts)
                for sym, qty in positions.items():
                    if qty <= 0: continue
                    if sym in self.hedged: continue
                    bid,ask = _best(self.client.get_book(sym) or {})
                    if not bid or not ask: continue
                    S = 0.5*(bid+ask)
                    notional = qty * S
                    if notional < min_notional: continue
                    puts = puts_by_und.get(sym)
                    if not puts: continue
                    target_k = PROTECT_STRIKE_FRAC * S
                    puts.sort(key=lambda t: abs(t[0] - target_k))
                    strike, psym = puts[0]
                    pb, pa = _best(self.client.get_book(psym) or {})
                    if not pa: continue
                    premium_total = pa * qty
                    if premium_total > MAX_PREMIUM_PCT * notional: continue
                    try:
                        self.client.buy(psym, round(pa,4), round(qty,4))
                        self.hedged.add(sym)
                        log.info("PROTECT %s pos=%.2f put=%s K=%.2f cost=%.2f", sym, qty, psym, strike, premium_total)
                    except Exception as e: log.warning("protect %s: %s", sym, e)

                time.sleep(POLL_SEC)
            except Exception as e:
                log.exception("loop: %s", e); time.sleep(POLL_SEC)


if __name__=="__main__":
    ProtectivePut().run()
