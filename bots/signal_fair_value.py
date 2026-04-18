"""
signal_fair_value.py — public-timeseries → spot fair-value regression (§3.6 analog).

Empirical analysis of the captured order-book + timeseries data (see
analyze_edges.py) showed several public series are near-deterministic drivers
of specific tradable symbols, e.g. grid_load→LIVI (corr 0.995, Δcorr 0.80),
bus_delay→RITE (0.92/0.94), scarlet_hype→HILL (0.81/0.74), dining_inventory→
LIVI, course_pressure→PASS/SCIX, student_spend→PASS. This bot discovers those
relationships online, fits a rolling OLS model per symbol, and fades the mid
toward the regression's fair value.

Design:
  * For every (symbol, public_signal) pair, maintain aligned deques of
    (mid, signal_value) sampled once per tick.
  * Periodically refit OLS mid = α + β·signal. Accept the fit only when both
    level-R² and first-difference-R² clear thresholds (guards against spurious
    cointegration from shared drift).
  * Pick the best-fitting signal per symbol. fair_now = α + β·signal_now.
  * Trade: if mid - fair < -k·σ_resid → long; if > +k·σ_resid → short.
  * Exit: |mid - fair| < exit_k·σ_resid, or MAX_HOLD_TICKS, or fit degrades.
  * Sizing: POS_PCT of capital per active symbol; hard inventory cap per name.
  * Bailout guard, halted-asset skip, discovery cadence mirror the fleet.
"""

import os
import time
import logging
from collections import defaultdict, deque

import numpy as np

from knight_trader import ExchangeClient

log = logging.getLogger("signal_fair_value")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

# Model & trading parameters
WINDOW = 120                 # samples kept for fitting
MIN_FIT_N = 30               # need at least this many aligned samples to fit
REFIT_EVERY_TICKS = 25       # refit cadence
FIT_LEVEL_R2_MIN = 0.40      # |corr|>=0.63 on levels
FIT_DIFF_R2_MIN = 0.10       # Δ-R² floor to reject spurious cointegration
ENTRY_SIGMA = 1.5            # enter when |mid - fair| > ENTRY_SIGMA · σ_resid
EXIT_SIGMA = 0.35            # exit when inside EXIT_SIGMA · σ_resid
MAX_HOLD_TICKS = 60
POS_PCT = 0.05               # 5% of bot capital per new trade
MAX_INVENTORY_PCT = 0.15     # hard cap on inventory value per symbol
FEATURE_POLL_SEC = 2.0
DISCOVER_SEC = 30.0
TEAM_POLL_SEC = 2.0
BAILOUT_HALT_EQUITY = 150_000
TARGET_TYPES = {"spot", "equity", "equities", "forex", "fx"}
SKIP_SIGNALS = {"ior_rate"}  # system feeds that don't drive prices


def _num(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _asset_type(a): return (a.get("asset_type") or a.get("type") or "").lower()
def _symbol_of(a): return a.get("symbol") or a.get("id") or a.get("name")


def _best_prices(book):
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bp = [_num(k) for k in bids.keys() if _num(k) > 0]
    ap = [_num(k) for k in asks.keys() if _num(k) > 0]
    return (max(bp) if bp else None, min(ap) if ap else None)


def _team_equity(ts):
    for k in ("total_equity", "equity", "leaderboard_equity", "net_equity"):
        if k in ts:
            return _num(ts[k])
    return 0.0


def _bot_capital(ts, bid):
    bots = ts.get("bots") or ts.get("portfolio", {}).get("bots") or {}
    b = bots.get(bid, {}) if isinstance(bots, dict) else next(
        (x for x in bots if x.get("id") == bid or x.get("bot_id") == bid), {})
    return _num(b.get("capital") or b.get("allocated_capital") or b.get("uncommitted_capital"))


def _bot_position(ts, bid, sym):
    bots = ts.get("bots") or ts.get("portfolio", {}).get("bots") or {}
    b = bots.get(bid, {}) if isinstance(bots, dict) else next(
        (x for x in bots if x.get("id") == bid or x.get("bot_id") == bid), {})
    raw = b.get("positions") or b.get("inventory") or {}
    if isinstance(raw, dict):
        v = raw.get(sym) or {}
        return _num(v.get("quantity") or v.get("qty") or v.get("size")) if isinstance(v, dict) else _num(v)
    if isinstance(raw, list):
        for row in raw:
            if row.get("symbol") == sym or row.get("asset") == sym:
                return _num(row.get("quantity") or row.get("qty") or row.get("size"))
    return 0.0


def _ols(x: np.ndarray, y: np.ndarray):
    # Returns (alpha, beta, r2, resid_std) or None if ill-conditioned.
    if len(x) < 5:
        return None
    mx, my = x.mean(), y.mean()
    dx, dy = x - mx, y - my
    vx = float((dx * dx).sum())
    if vx <= 1e-12:
        return None
    beta = float((dx * dy).sum() / vx)
    alpha = float(my - beta * mx)
    resid = y - (alpha + beta * x)
    ss_tot = float((dy * dy).sum())
    if ss_tot <= 0:
        return None
    r2 = 1.0 - float((resid * resid).sum()) / ss_tot
    return alpha, beta, r2, float(resid.std(ddof=0))


class SignalFairValue:
    def __init__(self):
        self.client = ExchangeClient()
        self.symbols: list[str] = []
        self.feature_names: list[str] = []
        # Aligned per-tick samples. Indexed by a shared append counter: we
        # append mid and every available feature value together each tick, so
        # the tail of both deques lines up by index.
        self.mid_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
        self.feat_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
        # Best model per symbol.
        self.models: dict[str, dict] = {}
        self.hold_age: dict[str, int] = {}
        self.tick = 0
        self.last_discover = 0.0
        self.last_feat_fetch = 0.0
        self.last_team = 0.0
        self.last_refit_tick = 0

    # ---- discovery ----
    def discover(self, now: float):
        if now - self.last_discover < DISCOVER_SEC and self.symbols:
            return
        try:
            assets = self.client.get_assets() or []
        except Exception as exc:
            log.warning("get_assets: %s", exc)
            return
        if isinstance(assets, dict):
            assets = list(assets.values())
        self.symbols = [
            _symbol_of(a) for a in assets
            if _asset_type(a) in TARGET_TYPES and not a.get("halted") and _symbol_of(a)
        ][:20]

        try:
            series = self.client.list_timeseries() or []
        except Exception:
            series = []
        names = []
        for s in series:
            n = s.get("name") if isinstance(s, dict) else str(s)
            if n and n not in SKIP_SIGNALS:
                names.append(n)
        self.feature_names = names[:12]
        self.last_discover = now
        log.info("tracking %d symbols, %d features", len(self.symbols), len(self.feature_names))

    # ---- sampling ----
    def _latest_feature(self, name: str):
        try:
            pts = self.client.get_timeseries(name, limit=1) or []
        except Exception:
            return None
        if not pts:
            return None
        last = pts[-1]
        v = last.get("v") if isinstance(last, dict) else last
        return _num(v, default=float("nan"))

    def sample(self, state: dict, now: float):
        books = state.get("book") or state.get("books") or {}
        # Fetch features at a slower cadence; hold last value in between.
        feat_now: dict[str, float] = {}
        if now - self.last_feat_fetch >= FEATURE_POLL_SEC:
            for f in self.feature_names:
                v = self._latest_feature(f)
                if v is not None and not np.isnan(v):
                    feat_now[f] = v
            self.last_feat_fetch = now
        else:
            # Reuse last observed feature to keep alignment with mids.
            for f in self.feature_names:
                dq = self.feat_hist.get(f)
                if dq:
                    feat_now[f] = dq[-1]
        if not feat_now:
            return

        # Append mids and features in lock-step.
        sampled_any = False
        for sym in self.symbols:
            bid, ask = _best_prices(books.get(sym) or {})
            if not bid or not ask:
                continue
            mid = 0.5 * (bid + ask)
            self.mid_hist[sym].append(mid)
            sampled_any = True
        if sampled_any:
            for f, v in feat_now.items():
                self.feat_hist[f].append(v)

    # ---- fitting ----
    def refit(self):
        for sym in self.symbols:
            mids = list(self.mid_hist.get(sym, []))
            if len(mids) < MIN_FIT_N:
                self.models.pop(sym, None)
                continue
            best = None
            for f in self.feature_names:
                vals = list(self.feat_hist.get(f, []))
                n = min(len(mids), len(vals))
                if n < MIN_FIT_N:
                    continue
                y = np.asarray(mids[-n:], dtype=float)
                x = np.asarray(vals[-n:], dtype=float)
                fit = _ols(x, y)
                if fit is None:
                    continue
                alpha, beta, r2, resid_std = fit
                if r2 < FIT_LEVEL_R2_MIN or resid_std <= 0:
                    continue
                # First-difference guard: reject pairs whose correlation is
                # driven by shared drift rather than contemporaneous moves.
                dy = np.diff(y)
                dx = np.diff(x)
                d_fit = _ols(dx, dy)
                if d_fit is None:
                    continue
                _, _, d_r2, _ = d_fit
                if d_r2 < FIT_DIFF_R2_MIN:
                    continue
                score = r2 * max(d_r2, 0.0)
                if best is None or score > best["score"]:
                    best = {
                        "feat": f, "alpha": alpha, "beta": beta,
                        "r2": r2, "diff_r2": d_r2, "resid_std": resid_std,
                        "n": n, "score": score,
                    }
            if best:
                self.models[sym] = best
                log.info("fit %s ← %s r2=%.2f Δr2=%.2f σ=%.3f β=%.3f",
                         sym, best["feat"], best["r2"], best["diff_r2"],
                         best["resid_std"], best["beta"])
            else:
                self.models.pop(sym, None)

    # ---- trading ----
    def fair_value(self, sym: str):
        m = self.models.get(sym)
        if not m:
            return None
        vals = self.feat_hist.get(m["feat"])
        if not vals:
            return None
        return m["alpha"] + m["beta"] * vals[-1], m["resid_std"]

    def _inventory_cap(self, capital: float, mid: float) -> float:
        return (MAX_INVENTORY_PCT * capital) / mid if mid > 0 else 0.0

    def trade(self, sym: str, book: dict, team_state: dict, capital: float):
        fv = self.fair_value(sym)
        if fv is None:
            # No usable model — flatten any stale inventory.
            pos = _bot_position(team_state, BOT_ID, sym)
            if pos != 0:
                bid, ask = _best_prices(book)
                if bid and ask:
                    self._flatten(sym, bid, ask, pos)
            return
        fair, sigma = fv
        bid, ask = _best_prices(book)
        if not bid or not ask or sigma <= 0:
            return
        mid = 0.5 * (bid + ask)
        pos = _bot_position(team_state, BOT_ID, sym)
        self.hold_age[sym] = self.hold_age.get(sym, 0) + 1 if pos != 0 else 0

        gap = mid - fair
        inv_cap = self._inventory_cap(capital, mid)
        qty = round(min(inv_cap, (POS_PCT * capital) / mid), 4)
        if qty <= 0:
            return

        # Exit logic first.
        if pos != 0:
            if abs(gap) < EXIT_SIGMA * sigma or self.hold_age.get(sym, 0) > MAX_HOLD_TICKS:
                self._flatten(sym, bid, ask, pos)
                self.hold_age[sym] = 0
                return

        # Entry logic: mean-revert mid toward fair.
        if gap < -ENTRY_SIGMA * sigma and pos < inv_cap:
            try:
                self.client.buy(sym, round(ask, 4), qty)
                self.hold_age[sym] = 1
                log.info("LONG  %s @ %.4f fair=%.4f σ=%.3f gap=%.3f",
                         sym, ask, fair, sigma, gap)
            except Exception as exc:
                log.warning("buy %s: %s", sym, exc)
        elif gap > ENTRY_SIGMA * sigma and pos > -inv_cap:
            try:
                self.client.sell(sym, round(bid, 4), qty)
                self.hold_age[sym] = 1
                log.info("SHORT %s @ %.4f fair=%.4f σ=%.3f gap=%.3f",
                         sym, bid, fair, sigma, gap)
            except Exception as exc:
                log.warning("sell %s: %s", sym, exc)

    def _flatten(self, sym: str, bid: float, ask: float, pos: float):
        try:
            if pos > 0:
                self.client.sell(sym, round(bid, 4), round(pos, 4))
            else:
                self.client.buy(sym, round(ask, 4), round(-pos, 4))
            log.info("EXIT  %s pos=%.4f", sym, pos)
        except Exception as exc:
            log.warning("exit %s: %s", sym, exc)

    # ---- main loop ----
    def run(self):
        team_state: dict = {}
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                self.tick += 1
                if state.get("competition_state") != "live":
                    continue
                self.discover(now)
                if not self.symbols or not self.feature_names:
                    continue

                self.sample(state, now)

                if self.tick - self.last_refit_tick >= REFIT_EVERY_TICKS:
                    self.refit()
                    self.last_refit_tick = self.tick

                if now - self.last_team > TEAM_POLL_SEC:
                    try:
                        team_state = self.client.get_team_state() or {}
                    except Exception:
                        pass
                    self.last_team = now

                eq = _team_equity(team_state)
                if eq and eq < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT GUARD equity=%.0f — halting", eq)
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
                    if book:
                        self.trade(sym, book, team_state, capital)
            except Exception as exc:
                log.exception("loop: %s", exc)
                time.sleep(1.0)


if __name__ == "__main__":
    SignalFairValue().run()
