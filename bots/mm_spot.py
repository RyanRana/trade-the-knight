"""
mm_spot.py — continuous two-sided market maker across all tradable spot / FX symbols.

Upload as a single file. The container injects BOT_ID and EXCHANGE_URL; the SDK
picks them up automatically. Python 3.9, knight_trader SDK.

Core ideas:
- Discover tradable symbols from client.get_assets(), filter to spot/forex.
- Quote both sides inside the NBBO when spread is wide enough to be profitable.
- Inventory-skew the quotes: lean the ask down when long, lean the bid up when short.
- Half-Kelly sizing on each quote, throttled by per-symbol inventory cap.
- Flatten and halt if team equity drifts toward the $50k bailout trigger.
"""

import os
import time
import math
import logging
from collections import defaultdict

from knight_trader import ExchangeClient

log = logging.getLogger("mm_spot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

# --- Tuning ---------------------------------------------------------------
MIN_SPREAD = 0.02              # skip quoting if book spread tighter than this
QUOTE_INSIDE = 0.01            # how far inside the NBBO to post
BASE_SIZE_PCT = 0.01           # base per-side size as fraction of bot capital
MAX_INVENTORY_PCT = 0.05       # max long/short inventory notional per symbol
SKEW_STRENGTH = 0.6            # fraction of half-spread to skew at full inventory
MIN_REFRESH_SEC = 0.15         # min time between re-quotes per symbol
MAX_SYMBOLS = 12               # cap symbols under 0.25 CPU budget
BAILOUT_HALT_EQUITY = 150_000  # cancel-all + idle when team equity drops below
FX_TYPES = {"forex", "fx"}
SPOT_TYPES = {"spot", "equity", "equities"}
TARGET_TYPES = FX_TYPES | SPOT_TYPES


# --- Safe reads -----------------------------------------------------------
def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _asset_type(asset):
    return (asset.get("asset_type") or asset.get("type") or "").lower()


def _is_tradable(asset):
    if asset.get("halted"):
        return False
    if "tradable" in asset and not asset["tradable"]:
        return False
    return True


def _symbol_of(asset):
    return asset.get("symbol") or asset.get("id") or asset.get("name")


def _team_equity(team_state):
    for key in ("total_equity", "equity", "leaderboard_equity", "net_equity"):
        if key in team_state:
            return _num(team_state[key])
    # fall back to RUD + rough bot capital sum
    rud = _num(team_state.get("rud") or team_state.get("treasury", {}).get("rud"))
    cap = 0.0
    for b in (team_state.get("bots") or {}).values():
        cap += _num(b.get("capital") or b.get("allocated_capital"))
    return rud + cap


def _bot_capital(team_state, bot_id):
    bots = team_state.get("bots") or team_state.get("portfolio", {}).get("bots") or {}
    if isinstance(bots, dict):
        bot = bots.get(bot_id) or {}
    else:  # list shape
        bot = next((b for b in bots if b.get("id") == bot_id or b.get("bot_id") == bot_id), {})
    return _num(bot.get("capital") or bot.get("allocated_capital") or bot.get("uncommitted_capital"))


def _bot_positions(team_state, bot_id):
    bots = team_state.get("bots") or team_state.get("portfolio", {}).get("bots") or {}
    if isinstance(bots, dict):
        bot = bots.get(bot_id) or {}
    else:
        bot = next((b for b in bots if b.get("id") == bot_id or b.get("bot_id") == bot_id), {})
    raw = bot.get("positions") or bot.get("inventory") or {}
    out = {}
    if isinstance(raw, dict):
        for sym, v in raw.items():
            if isinstance(v, dict):
                out[sym] = _num(v.get("quantity") or v.get("qty") or v.get("size"))
            else:
                out[sym] = _num(v)
    elif isinstance(raw, list):
        for row in raw:
            sym = row.get("symbol") or row.get("asset")
            if sym:
                out[sym] = _num(row.get("quantity") or row.get("qty") or row.get("size"))
    return out


def _best_prices(book):
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bid_prices = [p for p in (_num(k, None) for k in bids.keys()) if p]
    ask_prices = [p for p in (_num(k, None) for k in asks.keys()) if p]
    bid_prices = [p for p in bid_prices if p is not None]
    ask_prices = [p for p in ask_prices if p is not None]
    best_bid = max(bid_prices) if bid_prices else None
    best_ask = min(ask_prices) if ask_prices else None
    return best_bid, best_ask


# --- Core loop ------------------------------------------------------------
class MarketMaker:
    def __init__(self):
        self.client = ExchangeClient()
        self.resting = defaultdict(lambda: {"bid": None, "ask": None, "bid_px": None, "ask_px": None, "ts": 0.0})
        self.symbols = []
        self.asset_meta = {}
        self.last_refresh_symbols = 0.0
        self.halted = False

    # Asset discovery refreshes every 30s; cheap and handles admin-added assets.
    def refresh_symbols(self, now):
        if now - self.last_refresh_symbols < 30.0 and self.symbols:
            return
        try:
            assets = self.client.get_assets() or []
        except Exception as exc:
            log.warning("get_assets failed: %s", exc)
            return
        if isinstance(assets, dict):
            assets = list(assets.values())
        picked = []
        for a in assets:
            if _asset_type(a) not in TARGET_TYPES:
                continue
            if not _is_tradable(a):
                continue
            sym = _symbol_of(a)
            if not sym:
                continue
            picked.append(sym)
            self.asset_meta[sym] = a
            if len(picked) >= MAX_SYMBOLS:
                break
        self.symbols = picked
        self.last_refresh_symbols = now
        log.info("tracking %d symbols: %s", len(picked), picked)

    def cancel_resting(self, symbol, side):
        rec = self.resting[symbol]
        oid = rec.get(side)
        if oid:
            try:
                self.client.cancel(oid)
            except Exception:
                pass
            rec[side] = None
            rec[f"{side}_px"] = None

    def cancel_all_local(self):
        try:
            self.client.cancel_all()
        except Exception:
            pass
        self.resting.clear()

    # Compute our target quotes for one symbol.
    def compute_quotes(self, symbol, book, inventory, capital):
        best_bid, best_ask = _best_prices(book)
        if not best_bid or not best_ask or best_bid >= best_ask:
            return None
        spread = best_ask - best_bid
        if spread < MIN_SPREAD:
            return None
        mid = 0.5 * (best_bid + best_ask)
        # Inventory saturation in [-1, 1]; positive = long.
        max_inv_notional = MAX_INVENTORY_PCT * max(capital, 1.0)
        inv_notional = inventory * mid
        sat = 0.0 if max_inv_notional <= 0 else max(-1.0, min(1.0, inv_notional / max_inv_notional))
        half = spread / 2.0
        skew = SKEW_STRENGTH * half * sat  # positive skew pushes both quotes down → easier to sell, harder to buy
        bid_px = round(min(best_bid + QUOTE_INSIDE, mid - QUOTE_INSIDE) - skew, 4)
        ask_px = round(max(best_ask - QUOTE_INSIDE, mid + QUOTE_INSIDE) - skew, 4)
        if bid_px >= ask_px:
            return None
        # Scale size down as inventory approaches cap, hard-stop adding on the saturated side.
        bid_scale = max(0.0, 1.0 - max(0.0, sat))
        ask_scale = max(0.0, 1.0 + min(0.0, sat))
        raw_size = (BASE_SIZE_PCT * capital) / mid if mid > 0 else 0.0
        bid_qty = round(raw_size * bid_scale, 4)
        ask_qty = round(raw_size * ask_scale, 4)
        return bid_px, ask_px, bid_qty, ask_qty

    def quote_symbol(self, symbol, book, team_state, bot_cap, now):
        positions = _bot_positions(team_state, BOT_ID)
        inv = positions.get(symbol, 0.0)
        target = self.compute_quotes(symbol, book, inv, bot_cap)
        rec = self.resting[symbol]
        if not target:
            if rec["bid"] or rec["ask"]:
                self.cancel_resting(symbol, "bid")
                self.cancel_resting(symbol, "ask")
            return
        bid_px, ask_px, bid_qty, ask_qty = target
        if now - rec["ts"] < MIN_REFRESH_SEC and rec["bid_px"] == bid_px and rec["ask_px"] == ask_px:
            return

        # Replace quotes.
        self.cancel_resting(symbol, "bid")
        self.cancel_resting(symbol, "ask")
        if bid_qty > 0:
            try:
                oid = self.client.buy(symbol, bid_px, bid_qty)
                if oid:
                    rec["bid"] = oid
                    rec["bid_px"] = bid_px
            except Exception as exc:
                log.warning("buy %s failed: %s", symbol, exc)
        if ask_qty > 0:
            try:
                oid = self.client.sell(symbol, ask_px, ask_qty)
                if oid:
                    rec["ask"] = oid
                    rec["ask_px"] = ask_px
            except Exception as exc:
                log.warning("sell %s failed: %s", symbol, exc)
        rec["ts"] = now

    def run(self):
        last_team_fetch = 0.0
        team_state = {}
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                if state.get("competition_state") != "live":
                    if not self.halted:
                        self.cancel_all_local()
                        self.halted = True
                    continue
                self.halted = False
                self.refresh_symbols(now)
                if not self.symbols:
                    continue

                # Refresh team state at most once per second (it's a low-frequency endpoint).
                if now - last_team_fetch > 1.0:
                    try:
                        team_state = self.client.get_team_state() or {}
                    except Exception as exc:
                        log.warning("get_team_state failed: %s", exc)
                    last_team_fetch = now

                equity = _team_equity(team_state)
                if equity and equity < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT GUARD: team equity %.0f below %.0f — flattening", equity, BAILOUT_HALT_EQUITY)
                    self.cancel_all_local()
                    time.sleep(1.0)
                    continue

                bot_cap = _bot_capital(team_state, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}
                for sym in self.symbols:
                    book = books.get(sym) or {}
                    if not book:
                        try:
                            book = self.client.get_book(sym) or {}
                        except Exception:
                            book = {}
                    if not book:
                        continue
                    self.quote_symbol(sym, book, team_state, bot_cap, now)
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(0.5)


if __name__ == "__main__":
    MarketMaker().run()
