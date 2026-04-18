"""
pred_mispricing.py — trades YES-share prediction markets when the market price
diverges from our probability estimate by more than an edge threshold.

Probability estimate strategy (in priority order):
1. If there's a public timeseries whose name matches the prediction symbol,
   use its latest value (clipped to [0.01, 0.99]) as the probability.
2. If the underlying has been drifting, use an EMA of recent trade prices as
   a weak prior (assumes the market itself is reasonably calibrated).
3. Otherwise, skip — we don't trade without a prior.

Sizing: half-Kelly with a hard cap of 8% of bot capital per market.
"""

import os
import time
import logging

from knight_trader import ExchangeClient

log = logging.getLogger("pred_mispricing")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

EDGE_THRESHOLD = 0.12          # |p - price| must exceed this to trade
KELLY_FRACTION = 0.5           # half-Kelly
MAX_PCT_PER_MARKET = 0.08      # hard cap as fraction of bot capital
MIN_PRICE = 0.01
MAX_PRICE = 0.99
REFRESH_SYMBOLS_SEC = 30.0
BAILOUT_HALT_EQUITY = 150_000
PRED_TYPES = {"prediction", "prediction_market", "yes_share", "pm"}


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _asset_type(asset):
    return (asset.get("asset_type") or asset.get("type") or "").lower()


def _symbol_of(asset):
    return asset.get("symbol") or asset.get("id") or asset.get("name")


def _team_equity(team_state):
    for key in ("total_equity", "equity", "leaderboard_equity", "net_equity"):
        if key in team_state:
            return _num(team_state[key])
    return 0.0


def _bot_capital(team_state, bot_id):
    bots = team_state.get("bots") or team_state.get("portfolio", {}).get("bots") or {}
    if isinstance(bots, dict):
        bot = bots.get(bot_id) or {}
    else:
        bot = next((b for b in bots if b.get("id") == bot_id or b.get("bot_id") == bot_id), {})
    return _num(bot.get("capital") or bot.get("allocated_capital") or bot.get("uncommitted_capital"))


def _bot_position(team_state, bot_id, symbol):
    bots = team_state.get("bots") or team_state.get("portfolio", {}).get("bots") or {}
    if isinstance(bots, dict):
        bot = bots.get(bot_id) or {}
    else:
        bot = next((b for b in bots if b.get("id") == bot_id or b.get("bot_id") == bot_id), {})
    raw = bot.get("positions") or bot.get("inventory") or {}
    if isinstance(raw, dict):
        v = raw.get(symbol) or {}
        if isinstance(v, dict):
            return _num(v.get("quantity") or v.get("qty") or v.get("size"))
        return _num(v)
    if isinstance(raw, list):
        for row in raw:
            if row.get("symbol") == symbol or row.get("asset") == symbol:
                return _num(row.get("quantity") or row.get("qty") or row.get("size"))
    return 0.0


def _best_prices(book):
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bid_prices = [_num(k) for k in bids.keys()]
    ask_prices = [_num(k) for k in asks.keys()]
    bid_prices = [p for p in bid_prices if p > 0]
    ask_prices = [p for p in ask_prices if p > 0]
    return (max(bid_prices) if bid_prices else None,
            min(ask_prices) if ask_prices else None)


class PredictionTrader:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols = []
        self.asset_meta = {}
        self.last_discovery = 0.0
        self.timeseries_names = set()
        self.last_ts_refresh = 0.0

    def discover(self, now):
        if now - self.last_discovery < REFRESH_SYMBOLS_SEC and self.symbols:
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
            if _asset_type(a) in PRED_TYPES:
                sym = _symbol_of(a)
                if not sym:
                    continue
                picked.append(sym)
                self.asset_meta[sym] = a
        self.symbols = picked
        self.last_discovery = now
        log.info("tracking %d prediction markets: %s", len(picked), picked)

        if now - self.last_ts_refresh > 60.0:
            try:
                series = self.client.list_timeseries() or []
            except Exception:
                series = []
            self.timeseries_names = {
                (s.get("name") if isinstance(s, dict) else str(s)) for s in series
            }
            self.last_ts_refresh = now

    def probability_estimate(self, symbol):
        # 1. Direct timeseries match.
        candidates = [symbol, symbol.lower(), f"{symbol}_prob", f"prob_{symbol}"]
        for name in candidates:
            if name in self.timeseries_names:
                try:
                    pts = self.client.get_timeseries(name, limit=1) or []
                    if pts:
                        last = pts[-1]
                        v = last.get("v") if isinstance(last, dict) else last
                        p = _num(v)
                        if 0 < p <= 1:
                            return max(MIN_PRICE, min(MAX_PRICE, p))
                        if 1 < p <= 100:  # expressed as percent
                            return max(MIN_PRICE, min(MAX_PRICE, p / 100.0))
                except Exception:
                    pass
        # 2. Metadata hints.
        meta = self.asset_meta.get(symbol) or {}
        for key in ("probability", "prob", "fair_value", "expected_value"):
            if key in meta:
                p = _num(meta[key])
                if 0 < p <= 1:
                    return max(MIN_PRICE, min(MAX_PRICE, p))
        return None

    def target_size(self, p, price, side, capital):
        # Kelly fraction for YES share at price `price` with true prob `p`:
        # payout b = (1 - price) / price for a long, = price / (1 - price) for a short.
        if side == "long":
            b = (1 - price) / price
            f = (p * (b + 1) - 1) / b
        else:
            b = price / (1 - price)
            f = ((1 - p) * (b + 1) - 1) / b
        f = max(0.0, min(MAX_PCT_PER_MARKET, KELLY_FRACTION * f))
        notional = f * capital
        qty = notional / price if side == "long" else notional / (1 - price)
        return round(qty, 2)

    def trade_market(self, symbol, book, team_state, capital):
        best_bid, best_ask = _best_prices(book)
        if not best_bid or not best_ask:
            return
        p = self.probability_estimate(symbol)
        if p is None:
            return
        pos = _bot_position(team_state, BOT_ID, symbol)

        # Long opportunity: our p is meaningfully above the ask.
        if p - best_ask > EDGE_THRESHOLD:
            desired = self.target_size(p, best_ask, "long", capital)
            to_buy = desired - max(pos, 0.0)
            if to_buy > 0.5:
                try:
                    self.client.buy(symbol, round(best_ask, 2), round(to_buy, 2))
                    log.info("BUY %s qty=%.2f @ %.2f (p=%.2f)", symbol, to_buy, best_ask, p)
                except Exception as exc:
                    log.warning("buy %s failed: %s", symbol, exc)

        # Short opportunity: our p is meaningfully below the bid.
        elif best_bid - p > EDGE_THRESHOLD:
            desired = self.target_size(p, best_bid, "short", capital)
            current_short = -min(pos, 0.0)
            to_sell = desired - current_short
            if to_sell > 0.5:
                try:
                    self.client.sell(symbol, round(best_bid, 2), round(to_sell, 2))
                    log.info("SELL %s qty=%.2f @ %.2f (p=%.2f)", symbol, to_sell, best_bid, p)
                except Exception as exc:
                    log.warning("sell %s failed: %s", symbol, exc)

        # Convergence: unwind when price approaches our estimate.
        elif abs(best_bid if pos < 0 else best_ask - p) < 0.03 and pos != 0:
            try:
                if pos > 0:
                    self.client.sell(symbol, round(best_bid, 2), round(pos, 2))
                else:
                    self.client.buy(symbol, round(best_ask, 2), round(-pos, 2))
                log.info("UNWIND %s pos=%.2f near p=%.2f", symbol, pos, p)
            except Exception as exc:
                log.warning("unwind %s failed: %s", symbol, exc)

    def run(self):
        last_team_fetch = 0.0
        team_state = {}
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                if state.get("competition_state") != "live":
                    continue
                self.discover(now)
                if not self.symbols:
                    continue

                if now - last_team_fetch > 2.0:
                    try:
                        team_state = self.client.get_team_state() or {}
                    except Exception:
                        pass
                    last_team_fetch = now

                equity = _team_equity(team_state)
                if equity and equity < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT GUARD: equity %.0f — halting", equity)
                    try:
                        self.client.cancel_all()
                    except Exception:
                        pass
                    time.sleep(2.0)
                    continue

                capital = _bot_capital(team_state, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}
                for sym in self.symbols:
                    book = books.get(sym) or {}
                    if not book:
                        try:
                            book = self.client.get_book(sym) or {}
                        except Exception:
                            continue
                    if book:
                        self.trade_market(sym, book, team_state, capital)
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(1.0)


if __name__ == "__main__":
    PredictionTrader().run()
