"""
carry_vault.py — pure yield stack.

Ensembles three zero/low-directional strategies into one bot:

  (1) ior_parker     — hold RUD, earn Interest on Reserves (always on)
  (2) bond_auction   — bid in bond auctions at above-fair-yield prices
  (3) fx_carry       — when FX pairs show implied-rate differentials, tilt
                       long high-yield / short low-yield

The bot starts in pure-parking mode. When admins create bonds or FX pairs,
the corresponding overlay activates automatically. IOR keeps accruing the
whole time. This is the "hurdle rate" bot — guaranteed positive return
against any opponent that wastes capital on unprofitable trades.

Upload as single file. Container injects BOT_ID + EXCHANGE_URL.
"""

import logging
import os
import time
from collections import defaultdict, deque

from knight_trader import ExchangeClient

log = logging.getLogger("carry_vault")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

REPORT_SEC          = 30.0
FX_WINDOW           = 60
FX_CARRY_THRESHOLD  = 0.01     # differential > 1% to justify carry
FX_POS_PCT          = 0.05     # per-pair carry position
BOND_BID_EDGE       = 0.002    # bid yield = fair_yield + 0.2% (aggressive)
BOND_POS_PCT        = 0.10     # max per-bond exposure
BAILOUT_HALT_EQUITY = 25_000

# Outlier guard — reject aggressive FX carry executions >N% from recent prints.
SANE_MAX_DEV        = 0.05

BOND_TYPES = {"bond", "bonds", "fixed_income"}
FX_TYPES   = {"forex", "fx"}


def _num(x, d=0.0):
    try: return float(x)
    except (TypeError, ValueError): return d


def _asset_type(a): return (a.get("asset_type") or a.get("type") or "").lower()
def _sym(a): return a.get("symbol") or a.get("id") or a.get("name")
def _tradable(a):
    if a.get("halted"): return False
    if "tradable" in a and not a["tradable"]: return False
    return True


def _best(book):
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bp = [_num(k) for k in bids.keys() if _num(k) > 0]
    ap = [_num(k) for k in asks.keys() if _num(k) > 0]
    return (max(bp) if bp else None, min(ap) if ap else None)


def _sane_aggressive(px, prints, max_dev=SANE_MAX_DEV):
    """Reject crosses more than max_dev from mean(recent prints). Pass when no history."""
    if px is None or px <= 0:
        return False
    pxs = list(prints) if prints else []
    if not pxs:
        return True
    ref = sum(pxs) / len(pxs)
    if ref <= 0:
        return True
    return abs(px - ref) / ref <= max_dev


def _mean(xs): return sum(xs) / len(xs) if xs else 0.0


def _team_equity(t):
    for k in ("total_equity", "equity", "leaderboard_equity", "net_equity"):
        if k in t: return _num(t[k])
    rud = _num(t.get("rud") or t.get("treasury", {}).get("rud"))
    bots_raw = t.get("bots") or {}
    bots_iter = bots_raw.values() if isinstance(bots_raw, dict) else bots_raw
    cap = sum(_num(b.get("capital") or b.get("allocated_capital")) for b in bots_iter)
    return rud + cap


def _bot(t, bid):
    bots = t.get("bots") or t.get("portfolio", {}).get("bots") or {}
    if isinstance(bots, dict): return bots.get(bid) or {}
    return next((b for b in bots if b.get("id") == bid or b.get("bot_id") == bid), {})


def _bot_capital(t, bid):
    b = _bot(t, bid)
    return _num(b.get("capital") or b.get("allocated_capital") or b.get("uncommitted_capital"))


def _bot_positions(t, bid):
    raw = _bot(t, bid).get("positions") or _bot(t, bid).get("inventory") or {}
    out = {}
    if isinstance(raw, dict):
        for s, v in raw.items():
            out[s] = _num(v.get("quantity") or v.get("qty") or v.get("size")) if isinstance(v, dict) else _num(v)
    elif isinstance(raw, list):
        for r in raw:
            s = r.get("symbol") or r.get("asset")
            if s: out[s] = _num(r.get("quantity") or r.get("qty") or r.get("size"))
    return out


def _latest_ior(client):
    for name in ("ior_rate", "fed_rate"):
        try:
            pts = client.get_timeseries(name, limit=1) or []
            if pts:
                last = pts[-1]
                v = last.get("v") or last.get("value") if isinstance(last, dict) else last
                r = _num(v)
                if r > 0: return r
        except Exception: continue
    return 0.035


class CarryVault:
    def __init__(self):
        self.client = ExchangeClient()
        self.last_report = 0.0
        self.fx_prints = defaultdict(lambda: deque(maxlen=FX_WINDOW))
        self.bonds_seen = set()
        self.bonds_bid  = {}  # asset -> auction_id we bid on
        self.halted = False

    def fetch_assets(self):
        try: assets = self.client.get_assets() or []
        except Exception: return [], [], []
        if isinstance(assets, dict): assets = list(assets.values())
        bonds = [a for a in assets if _asset_type(a) in BOND_TYPES and _tradable(a)]
        fx    = [a for a in assets if _asset_type(a) in FX_TYPES   and _tradable(a)]
        return assets, bonds, fx

    # --- BOND OVERLAY ---------------------------------------------------
    def maybe_bid_bonds(self, bonds, cap, ior):
        for a in bonds:
            sym = _sym(a)
            if not sym: continue
            auction = a.get("auction") or a.get("auction_state") or {}
            status = (auction.get("status") or "").lower()
            if status not in ("open", "accepting_bids"): continue
            if sym in self.bonds_bid: continue
            maturity_ticks = _num(auction.get("maturity_ticks") or auction.get("T") or 365)
            fair_yield = ior + BOND_BID_EDGE
            # par = 1000; fair price = 1000 / (1+y)^T (T in years)
            years = maturity_ticks / 365.0 if maturity_ticks > 10 else maturity_ticks
            bid_price = round(1000.0 / ((1.0 + fair_yield) ** max(years, 0.01)), 4)
            max_notional = BOND_POS_PCT * cap
            qty = max(1, int(max_notional / bid_price))
            try:
                oid = self.client.place_auction_bid(sym, bid_price, qty) if hasattr(self.client, "place_auction_bid") else self.client.buy(sym, bid_price, qty)
                if oid:
                    self.bonds_bid[sym] = oid
                    log.info("BOND BID %s qty=%s @ %s (target_yield=%.4f, T=%.2fy)", sym, qty, bid_price, fair_yield, years)
            except Exception as exc:
                log.warning("bond bid %s failed: %s", sym, exc)

    # --- FX CARRY OVERLAY ----------------------------------------------
    def ingest_fx_trades(self, state, fx_syms):
        for t in (state.get("recent_trades") or state.get("trades") or []):
            s = t.get("symbol"); px = _num(t.get("price"))
            if s in fx_syms and px > 0:
                self.fx_prints[s].append(px)

    def fx_implied_yield(self, sym):
        px = list(self.fx_prints[sym])
        if len(px) < 10: return None
        # crude: percentage drift over window → implied differential
        return (px[0] - px[-1]) / px[-1] if px[-1] else None

    def fx_carry_trade(self, fx, books, cap):
        yields = {}
        for a in fx:
            s = _sym(a)
            y = self.fx_implied_yield(s)
            if y is not None: yields[s] = y
        if len(yields) < 2: return
        hi = max(yields.items(), key=lambda kv: kv[1])
        lo = min(yields.items(), key=lambda kv: kv[1])
        spread = hi[1] - lo[1]
        if spread < FX_CARRY_THRESHOLD: return
        # Long hi-yield, short lo-yield (size small; FX_POS_PCT each leg)
        for sym, sign in ((hi[0], +1), (lo[0], -1)):
            bb, ba = _best(books.get(sym) or {})
            if not bb or not ba: continue
            mid = 0.5 * (bb + ba)
            qty = round((FX_POS_PCT * cap) / mid, 4)
            if qty <= 0: continue
            exec_px = ba if sign > 0 else bb
            if not _sane_aggressive(exec_px, self.fx_prints[sym]):
                log.warning("FX carry %s skipped — %s=%s outside ±%.0f%% of recent prints",
                            sym, "ba" if sign > 0 else "bb", exec_px, SANE_MAX_DEV * 100)
                continue
            try:
                (self.client.buy if sign > 0 else self.client.sell)(sym, exec_px, qty)
                log.info("FX CARRY %s qty=%s @ %s (y=%.4f, spread=%.4f)", sym, sign * qty, exec_px, yields[sym], spread)
            except Exception as exc:
                log.warning("fx carry %s failed: %s", sym, exc)

    # --- IOR status ----------------------------------------------------
    def report(self, team, ior):
        eq = _team_equity(team)
        cap = _bot_capital(team, BOT_ID)
        daily = cap * ior / 365.0
        log.info("equity=%.0f bot_cap=%.0f ior=%.4f daily_accrual≈%.2f",
                 eq, cap, ior, daily)

    def flatten(self):
        try: self.client.cancel_all()
        except Exception: pass

    def run(self):
        last_team = 0.0
        team = {}
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                if state.get("competition_state") != "live":
                    if not self.halted: self.flatten(); self.halted = True
                    continue
                self.halted = False

                if now - last_team > 1.0:
                    try: team = self.client.get_team_state() or {}
                    except Exception as exc: log.warning("get_team_state: %s", exc)
                    last_team = now

                eq = _team_equity(team)
                if eq and eq < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT GUARD: equity %.0f — idle", eq)
                    self.flatten(); time.sleep(1.0); continue

                ior = _latest_ior(self.client)
                cap = _bot_capital(team, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}

                _, bonds, fx = self.fetch_assets()
                fx_syms = {_sym(a) for a in fx}
                self.ingest_fx_trades(state, fx_syms)

                if bonds: self.maybe_bid_bonds(bonds, cap, ior)
                if fx:    self.fx_carry_trade(fx, books, cap)

                if now - self.last_report > REPORT_SEC:
                    self.report(team, ior)
                    self.last_report = now
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(0.5)


if __name__ == "__main__":
    CarryVault().run()
