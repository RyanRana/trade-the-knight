"""In-process matching simulator + mock ExchangeClient.

Bots import `knight_trader.ExchangeClient`. We install a fake `knight_trader`
module in sys.modules before loading the bot so no code changes are needed.

Fill model:
- Taker fill on submit: buy() consumes ask levels up to limit_price; sell()
  consumes bid levels down to limit_price. Remaining qty rests.
- Resting fill from tape: when a trade prints at p on sym, any resting BUY at
  px >= p fills up to TAPE_FILL_FRACTION * print_qty at the resting px; mirror
  for sells.
- Resting fill on book update: if the new book crosses our resting order, we
  fill (taker-style) up to the cumulative opposite depth at or better than our
  price.

Accounting uses an average-cost basis per symbol with signed positions. Realized
PnL is booked on closing trades; unrealized is marked at last print (or mid).
"""
from __future__ import annotations
import os, sys, types, time as _real_time, math, random
from collections import defaultdict, deque
from typing import Optional, Any, Dict, List

TAPE_FILL_FRACTION = 0.5   # resting orders see at most half of each tape print


# ---------------------------------------------------------------------------
# Time patching — bots use time.monotonic() for throttles. We replace those
# with sim-time so the bot's refresh-interval gates fire at sim cadence.
# ---------------------------------------------------------------------------

class _TimeProxy:
    """Mutable sim clock exposed as time.monotonic / time.time / time.sleep."""

    def __init__(self):
        self.t = 0.0

    def set(self, v: float):
        self.t = v

    def advance(self, dt: float):
        self.t += max(0.0, dt)


_SIM_CLOCK = _TimeProxy()
_ORIG_MONOTONIC = _real_time.monotonic
_ORIG_TIME = _real_time.time
_ORIG_SLEEP = _real_time.sleep


def install_time_patch():
    _real_time.monotonic = lambda: _SIM_CLOCK.t
    _real_time.time = lambda: _SIM_CLOCK.t
    _real_time.sleep = lambda x: _SIM_CLOCK.advance(x if isinstance(x, (int, float)) else 0.0)


def restore_time():
    _real_time.monotonic = _ORIG_MONOTONIC
    _real_time.time = _ORIG_TIME
    _real_time.sleep = _ORIG_SLEEP


# ---------------------------------------------------------------------------
# Sim state
# ---------------------------------------------------------------------------

class SimState:
    def __init__(self, starting_cap: float = 1_000_000.0, ior_rate: float = 0.035):
        self.starting_cap = starting_cap
        self.cash = starting_cap
        self.ior_rate = ior_rate
        # Book: sym -> {"bids": {px: qty}, "asks": {px: qty}}  (px as str, qty as float)
        self.books: Dict[str, Dict[str, Dict[str, float]]] = {}
        self.assets: List[dict] = []
        self.timeseries: Dict[str, dict] = {}
        # Per-bot (single bot at a time)
        self.positions: Dict[str, float] = defaultdict(float)
        self.avg_cost: Dict[str, float] = defaultdict(float)  # cost basis of current open side
        self.open_orders: Dict[str, dict] = {}      # oid -> {sym, side, px, qty, placed_t}
        self.fills: List[dict] = []
        self.realized_pnl: float = 0.0
        self.last_trade_px: Dict[str, float] = {}
        # Rolling recent_trades exposed to the bot (new prints since last state).
        self.recent_trades_buffer: deque = deque(maxlen=200)
        self._oid_counter = 0

    # ----- book helpers -----
    def _best_bid(self, sym: str) -> Optional[float]:
        b = self.books.get(sym, {}).get("bids", {})
        if not b:
            return None
        return max(float(p) for p in b.keys())

    def _best_ask(self, sym: str) -> Optional[float]:
        a = self.books.get(sym, {}).get("asks", {})
        if not a:
            return None
        return min(float(p) for p in a.keys())

    def mid(self, sym: str) -> Optional[float]:
        bb = self._best_bid(sym)
        ba = self._best_ask(sym)
        if bb is not None and ba is not None:
            return 0.5 * (bb + ba)
        return self.last_trade_px.get(sym)

    # ----- order lifecycle -----
    def _next_oid(self) -> str:
        self._oid_counter += 1
        return f"SIM-{self._oid_counter:08d}"

    def submit(self, side: str, sym: str, price: float, qty: float) -> Optional[str]:
        if qty <= 0 or price <= 0 or not sym:
            return None
        # Track capital lock — reject if buy would overdraft available cash.
        if side == "buy":
            required = price * qty
            # Crude: reject if would push cash below 0 (allow shorts freely).
            if self.cash - required < -0.5 * self.starting_cap:
                return None
        oid = self._next_oid()
        remaining = self._taker_fill(oid, side, sym, price, qty)
        if remaining > 1e-9:
            self.open_orders[oid] = {
                "sym": sym, "side": side, "px": float(price),
                "qty": remaining, "placed_t": _SIM_CLOCK.t,
            }
        return oid

    def cancel(self, oid: str) -> bool:
        return self.open_orders.pop(oid, None) is not None

    def cancel_all(self):
        self.open_orders.clear()

    # ----- fill logic -----
    def _taker_fill(self, oid: str, side: str, sym: str, limit_px: float, qty: float) -> float:
        """Consume the opposing book up to limit_px. Returns leftover qty."""
        book = self.books.get(sym) or {}
        if side == "buy":
            levels = sorted(((float(p), q) for p, q in (book.get("asks") or {}).items()), key=lambda kv: kv[0])
            opposite = "asks"
        else:
            levels = sorted(((float(p), q) for p, q in (book.get("bids") or {}).items()), key=lambda kv: -kv[0])
            opposite = "bids"

        remaining = qty
        drained_keys = []
        updated = {}  # str_px -> new_qty
        for px, level_qty in levels:
            if remaining <= 0:
                break
            if side == "buy" and px > limit_px:
                break
            if side == "sell" and px < limit_px:
                break
            take = min(remaining, level_qty)
            self._book_fill(oid, side, sym, px, take)
            remaining -= take
            new_qty = level_qty - take
            # find the str key we consumed — book dict keys may be floats-as-str
            matched_key = None
            for k in (book.get(opposite) or {}).keys():
                try:
                    if abs(float(k) - px) < 1e-9:
                        matched_key = k
                        break
                except Exception:
                    pass
            if matched_key is not None:
                if new_qty <= 1e-9:
                    drained_keys.append(matched_key)
                else:
                    updated[matched_key] = new_qty

        # apply book mutations
        for k in drained_keys:
            book.get(opposite, {}).pop(k, None)
        for k, v in updated.items():
            book.setdefault(opposite, {})[k] = v
        return max(0.0, remaining)

    def _book_fill(self, oid: str, side: str, sym: str, px: float, qty: float):
        """Record a fill and update cash/positions/realized PnL."""
        if qty <= 0:
            return
        signed_qty = qty if side == "buy" else -qty
        old = self.positions.get(sym, 0.0)
        new = old + signed_qty
        # Update cash
        if side == "buy":
            self.cash -= px * qty
        else:
            self.cash += px * qty
        # Avg-cost accounting with sign flips.
        if old == 0.0 or (old > 0 and signed_qty > 0) or (old < 0 and signed_qty < 0):
            # Same direction: update average cost
            prev_abs = abs(old)
            add_abs = abs(signed_qty)
            total_abs = prev_abs + add_abs
            if total_abs > 0:
                self.avg_cost[sym] = (self.avg_cost[sym] * prev_abs + px * add_abs) / total_abs
        else:
            # Opposite direction: realize against existing inventory
            closing = min(abs(signed_qty), abs(old))
            if old > 0:
                # was long, selling -> realize (px - avg_cost) * closing
                self.realized_pnl += (px - self.avg_cost[sym]) * closing
            else:
                # was short, buying -> realize (avg_cost - px) * closing
                self.realized_pnl += (self.avg_cost[sym] - px) * closing
            # If flipped through zero, reset avg_cost to new entry px
            remaining_after_close = abs(signed_qty) - closing
            if remaining_after_close > 1e-9:
                self.avg_cost[sym] = px
            elif abs(new) < 1e-9:
                self.avg_cost[sym] = 0.0
        self.positions[sym] = new
        self.last_trade_px[sym] = px
        self.fills.append({
            "oid": oid, "t": _SIM_CLOCK.t, "sym": sym,
            "side": side, "px": px, "qty": qty,
        })

    # ----- events -----
    def apply_book_update(self, books: Dict[str, dict]):
        # Replace per-symbol books with the latest snapshot.
        for sym, b in books.items():
            self.books[sym] = {
                "bids": dict(b.get("bids") or {}),
                "asks": dict(b.get("asks") or {}),
            }
        # After the book moves, resting orders may now be crossable.
        self._check_resting_against_book()

    def _check_resting_against_book(self):
        filled = []
        for oid, od in list(self.open_orders.items()):
            sym, side, px, qty = od["sym"], od["side"], od["px"], od["qty"]
            if side == "buy":
                ba = self._best_ask(sym)
                if ba is not None and ba <= px and qty > 0:
                    leftover = self._taker_fill(oid, "buy", sym, px, qty)
                    if leftover <= 1e-9:
                        filled.append(oid)
                    else:
                        od["qty"] = leftover
            else:
                bb = self._best_bid(sym)
                if bb is not None and bb >= px and qty > 0:
                    leftover = self._taker_fill(oid, "sell", sym, px, qty)
                    if leftover <= 1e-9:
                        filled.append(oid)
                    else:
                        od["qty"] = leftover
        for oid in filled:
            self.open_orders.pop(oid, None)

    def apply_trade_print(self, trade: dict):
        sym = trade.get("symbol")
        px = float(trade.get("price") or 0)
        qty = float(trade.get("quantity") or 0)
        if not sym or px <= 0 or qty <= 0:
            return
        self.last_trade_px[sym] = px
        self.recent_trades_buffer.append({
            "symbol": sym, "price": str(px), "quantity": str(qty),
            "id": f"{trade.get('tick','')}-{sym}-{px}-{qty}",
            "tick": trade.get("tick", 0),
            "timestamp": trade.get("t_wall", _SIM_CLOCK.t),
        })
        # Fill resting orders touched by this print.
        available = qty * TAPE_FILL_FRACTION
        for oid, od in list(self.open_orders.items()):
            if od["sym"] != sym or available <= 0:
                continue
            if od["side"] == "buy" and od["px"] >= px:
                fill_qty = min(od["qty"], available)
                self._book_fill(oid, "buy", sym, od["px"], fill_qty)
                od["qty"] -= fill_qty
                available -= fill_qty
                if od["qty"] <= 1e-9:
                    self.open_orders.pop(oid, None)
            elif od["side"] == "sell" and od["px"] <= px:
                fill_qty = min(od["qty"], available)
                self._book_fill(oid, "sell", sym, od["px"], fill_qty)
                od["qty"] -= fill_qty
                available -= fill_qty
                if od["qty"] <= 1e-9:
                    self.open_orders.pop(oid, None)

    def apply_timeseries(self, series: Dict[str, dict]):
        for name, meta in series.items():
            # Store as a rolling list of {t, v} as the SDK would return.
            lst = self.timeseries.get(name, [])
            v = meta.get("latest_value")
            t = meta.get("latest_time")
            if v is not None:
                lst.append({"t": t, "v": v})
                if len(lst) > 500:
                    lst = lst[-500:]
            self.timeseries[name] = lst

    # ----- snapshot for bot consumption -----
    def snapshot_state(self) -> dict:
        """Return the dict yielded by stream_state() on this tick."""
        snap = {
            "competition_state": "live",
            "tick": int(_SIM_CLOCK.t * 10),  # synthetic monotonic tick
            "book": {s: {"bids": dict(b.get("bids", {})), "asks": dict(b.get("asks", {}))}
                     for s, b in self.books.items()},
            "recent_trades": list(self.recent_trades_buffer),
            "timeseries": [
                {"name": n, "latest_value": (lst[-1]["v"] if lst else None),
                 "latest_time": (lst[-1]["t"] if lst else None)}
                for n, lst in self.timeseries.items()
            ],
        }
        self.recent_trades_buffer = deque(maxlen=200)
        return snap

    # ----- valuation -----
    def equity(self) -> float:
        mtm = 0.0
        for sym, qty in self.positions.items():
            if abs(qty) < 1e-9:
                continue
            p = self.mid(sym) or self.last_trade_px.get(sym) or self.avg_cost.get(sym, 0.0)
            mtm += qty * p
        return self.cash + mtm


# ---------------------------------------------------------------------------
# Mock ExchangeClient — matches the bundled SDK surface the bots use.
# ---------------------------------------------------------------------------

class MockExchangeClient:
    def __init__(self, sim: SimState, bot_id: str = "SIMBOT"):
        self.sim = sim
        self.bot_id = bot_id
        self._feeder = None

    # Stream of states: set by the runner before bot.run() is called.
    def stream_state(self):
        assert self._feeder is not None, "feeder not set"
        for state in self._feeder:
            yield state

    def get_book(self, symbol: Optional[str] = None):
        if symbol is None:
            return {s: {"bids": dict(b.get("bids", {})), "asks": dict(b.get("asks", {}))}
                    for s, b in self.sim.books.items()}
        b = self.sim.books.get(symbol)
        if not b:
            return {}
        return {"bids": dict(b.get("bids", {})), "asks": dict(b.get("asks", {}))}

    def get_best_bid(self, symbol):
        return self.sim._best_bid(symbol)

    def get_best_ask(self, symbol):
        return self.sim._best_ask(symbol)

    def get_price(self, symbol):
        return self.sim.mid(symbol)

    def get_assets(self):
        return list(self.sim.assets)

    def get_team_state(self):
        pos = {s: {"quantity": q} for s, q in self.sim.positions.items() if abs(q) > 1e-9}
        return {
            "rud": self.sim.cash,
            "treasury": {"rud": self.sim.cash},
            "total_equity": self.sim.equity(),
            "bots": {
                self.bot_id: {
                    "id": self.bot_id,
                    "capital": self.sim.starting_cap,
                    "allocated_capital": self.sim.starting_cap,
                    "uncommitted_capital": max(0.0, self.sim.cash),
                    "positions": pos,
                }
            },
        }

    def list_timeseries(self):
        return [{"name": n} for n in self.sim.timeseries.keys()]

    def get_timeseries(self, name, limit=100):
        return list(self.sim.timeseries.get(name, []))[-limit:]

    def buy(self, symbol, price, quantity):
        return self.sim.submit("buy", symbol, float(price), float(quantity))

    def sell(self, symbol, price, quantity):
        return self.sim.submit("sell", symbol, float(price), float(quantity))

    def cancel(self, order_id):
        return self.sim.cancel(order_id)

    def cancel_all(self):
        self.sim.cancel_all()

    def place_auction_bid(self, symbol, yield_rate, quantity):
        # Sim doesn't model bond auctions; accept but don't fill.
        return None


def install_knight_trader_module(client: MockExchangeClient):
    """Install a fake `knight_trader` module so bots' imports resolve to our mock."""
    mod = types.ModuleType("knight_trader")
    mod.ExchangeClient = lambda *a, **kw: client
    sys.modules["knight_trader"] = mod


def drive_bot(feeder_generator, client: MockExchangeClient, bot_callable,
              step_time_dt: float = 0.0):
    """Run the bot's main loop driven by feeder_generator.

    feeder_generator yields dicts already shaped as state snapshots.
    bot_callable is e.g. bot_instance.run — a blocking call that internally
    iterates client.stream_state(). When the feeder exhausts, stream_state()
    ends and run() returns.
    """
    client._feeder = feeder_generator
    try:
        bot_callable()
    except StopIteration:
        pass
