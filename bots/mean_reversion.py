"""
mean_reversion.py — single-asset mean reversion (§3.9).

For each tradable spot/FX asset, track an EMA. Buy when price is meaningfully
below the EMA, sell when meaningfully above. Trend filter blocks signals when
the asset is > 2σ from a longer-window mean (regime guard).
"""

import os
import time
import logging
from collections import defaultdict, deque

import numpy as np

from knight_trader import ExchangeClient

log = logging.getLogger("mean_reversion")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

EMA_ALPHA = 2.0 / (20 + 1)         # 20-tick EMA
TREND_WINDOW = 100
ENTRY_DEV = 0.02                   # 2% from EMA
EXIT_DEV = 0.005
TREND_SKIP_SIGMA = 2.0
POS_PCT = 0.05
MAX_HOLD_TICKS = 30
BAILOUT_HALT_EQUITY = 150_000
TARGET_TYPES = {"spot", "equity", "equities", "forex", "fx"}


def _num(x, d=0.0):
    try: return float(x)
    except: return d

def _asset_type(a): return (a.get("asset_type") or a.get("type") or "").lower()
def _symbol_of(a): return a.get("symbol") or a.get("id") or a.get("name")

def _best_prices(book):
    bids = book.get("bids") or {}; asks = book.get("asks") or {}
    bp = [_num(k) for k in bids if _num(k) > 0]; ap = [_num(k) for k in asks if _num(k) > 0]
    return (max(bp) if bp else None, min(ap) if ap else None)

def _team_equity(ts):
    for k in ("total_equity","equity","leaderboard_equity"):
        if k in ts: return _num(ts[k])
    return 0.0

def _bot_capital(ts, bid):
    bots = ts.get("bots") or ts.get("portfolio",{}).get("bots") or {}
    b = bots.get(bid,{}) if isinstance(bots, dict) else next((x for x in bots if x.get("id")==bid),{})
    return _num(b.get("capital") or b.get("allocated_capital"))

def _bot_position(ts, bid, sym):
    bots = ts.get("bots") or ts.get("portfolio",{}).get("bots") or {}
    b = bots.get(bid,{}) if isinstance(bots, dict) else next((x for x in bots if x.get("id")==bid),{})
    raw = b.get("positions") or b.get("inventory") or {}
    if isinstance(raw, dict):
        v = raw.get(sym) or {}
        return _num(v.get("quantity") or v.get("qty") or v.get("size")) if isinstance(v, dict) else _num(v)
    return 0.0


class MeanReversion:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols = []
        self.emas = {}
        self.hist = defaultdict(lambda: deque(maxlen=TREND_WINDOW))
        self.holdings = {}  # sym -> opened_tick
        self.tick = 0
        self.last_discovery = 0.0

    def discover(self, now):
        if now - self.last_discovery < 30 and self.symbols: return
        try: assets = self.client.get_assets() or []
        except: return
        if isinstance(assets, dict): assets = list(assets.values())
        self.symbols = [_symbol_of(a) for a in assets
                        if _asset_type(a) in TARGET_TYPES and not a.get("halted") and _symbol_of(a)][:15]
        self.last_discovery = now

    def run(self):
        last_ts = 0.0; ts = {}
        for state in self.client.stream_state():
            try:
                now = time.monotonic(); self.tick += 1
                if state.get("competition_state") != "live": continue
                self.discover(now)
                if now - last_ts > 2.0:
                    try: ts = self.client.get_team_state() or {}
                    except: pass
                    last_ts = now
                if _team_equity(ts) and _team_equity(ts) < BAILOUT_HALT_EQUITY:
                    try: self.client.cancel_all()
                    except: pass
                    time.sleep(2); continue
                cap = _bot_capital(ts, BOT_ID) or 100_000
                books = state.get("book") or state.get("books") or {}
                for sym in self.symbols:
                    bid, ask = _best_prices(books.get(sym) or {})
                    if not bid or not ask: continue
                    mid = 0.5*(bid+ask)
                    self.hist[sym].append(mid)
                    prev = self.emas.get(sym, mid)
                    ema = prev + EMA_ALPHA * (mid - prev)
                    self.emas[sym] = ema

                    pos = _bot_position(ts, BOT_ID, sym)
                    age = self.tick - self.holdings.get(sym, self.tick)

                    # Exit if near EMA or hard stop
                    if pos != 0 and (abs(mid-ema)/ema < EXIT_DEV or age > MAX_HOLD_TICKS):
                        try:
                            if pos > 0: self.client.sell(sym, round(bid,4), round(pos,4))
                            else: self.client.buy(sym, round(ask,4), round(-pos,4))
                            self.holdings.pop(sym, None)
                        except Exception as e: log.warning("exit %s: %s", sym, e)
                        continue

                    if len(self.hist[sym]) < TREND_WINDOW: continue
                    arr = np.array(self.hist[sym])
                    trend_z = (mid - arr.mean()) / (arr.std() or 1e-9)
                    if abs(trend_z) > TREND_SKIP_SIGMA: continue  # strong trend, skip

                    qty = round((POS_PCT * cap) / mid, 4)
                    if qty <= 0: continue

                    if pos == 0:
                        if mid < ema * (1 - ENTRY_DEV):
                            try:
                                self.client.buy(sym, round(ask,4), qty)
                                self.holdings[sym] = self.tick
                                log.info("BUY %s @ %.4f ema=%.4f", sym, ask, ema)
                            except Exception as e: log.warning("buy %s: %s", sym, e)
                        elif mid > ema * (1 + ENTRY_DEV):
                            try:
                                self.client.sell(sym, round(bid,4), qty)
                                self.holdings[sym] = self.tick
                                log.info("SELL %s @ %.4f ema=%.4f", sym, bid, ema)
                            except Exception as e: log.warning("sell %s: %s", sym, e)
            except Exception as e:
                log.exception("loop: %s", e); time.sleep(1)


if __name__ == "__main__":
    MeanReversion().run()
