"""
qfc_sniper.py — QFC-only snapshot-scale mean-reversion sniper.

Replaces the failed `tick_sniper.py`. Root cause of that failure: the
original analysis measured reversion across *consecutive trades in a batch*
(microsecond-scale MM oscillation) rather than across *polling snapshots*
(~30s apart, which is the only timescale a polling bot can act on).

Re-run with proper snapshot-level time gating on data/trades.jsonl:

    sym    ACF1(snap)   trigger   n   win%   avg_ret   EV(bps)
    QFC      -0.641       1.0σ   14   86%    +7.68%     768
    QFC      -0.641       1.5σ    5  100%    +9.78%     978
    LIVI     +0.141        —     —    —        —         — (TRENDS at snap scale)
    HILL     -0.007        —     —    —        —         — (no signal)
    RITE     -0.226       1.5σ    4   75%    +2.83%     283 (thin sample, skip)

QFC is the ONLY symbol with a statistically real snapshot-scale reversion
edge. Everything else in tick_sniper's universe was noise or wrong-sign.

Execution model
---------------
Watch for new QFC prints in the state stream. When the latest trade price
differs from the last one we observed, compute return and update EWMA
sigma. If |ret| > TRIGGER_Z * sigma, aggress the opposite side.
Exit on next new observation (take whatever you get), on -8% hard stop
(p05 of unconditional QFC snap returns = -9.2%), or after MAX_HOLD_SEC.

Risk
----
- Single symbol, single open position at a time.
- Conservative 3% of capital per entry (edge is real but sample is 14
  events; don't oversize).
- Hard stop at -8% acknowledges QFC's real per-snapshot volatility.
- Bailout at $25k equity.
- Median outlier guard widened to 10% for QFC's native volatility.

Upload as single file. Container injects BOT_ID + EXCHANGE_URL.
"""

import logging
import math
import os
import time
from collections import deque

from knight_trader import ExchangeClient

log = logging.getLogger("qfc_sniper")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_ID = os.environ.get("BOT_ID", "")

SYMBOL = "QFC"

# Signal
TRIGGER_Z           = 1.0         # 86% WR at this level in backtest
SIGMA_EWMA_ALPHA    = 0.15        # ~7 effective samples (fast adapt)
MIN_OBS_FOR_SIG     = 8           # warmup

# Execution
SIZE_PCT            = 0.03        # 3% of capital per entry
HARD_STOP_PCT       = 0.08        # -8% (QFC p05 snap return = -9.2%)
MAX_HOLD_OBS        = 2           # exit after this many new observations
MAX_HOLD_SEC        = 90.0
MIN_RE_ENTRY_SEC    = 5.0

# Safety
BAILOUT_HALT_EQUITY = 25_000
SANE_MAX_DEV        = 0.10        # QFC native vol is high; 10% band
SANE_STOP_DEV       = 0.20        # on stop, must get out


def _num(x, d=0.0):
    try: return float(x)
    except (TypeError, ValueError): return d


def _best(book):
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bp = [_num(k) for k in bids.keys() if _num(k) > 0]
    ap = [_num(k) for k in asks.keys() if _num(k) > 0]
    return (max(bp) if bp else None, min(ap) if ap else None)


def _median(xs):
    s = sorted(xs); n = len(s)
    if n == 0: return 0.0
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _sane(px, prints, max_dev=SANE_MAX_DEV):
    if px is None or px <= 0: return False
    if not prints: return True
    ref = _median(prints)
    if ref <= 0: return True
    return abs(px - ref) / ref <= max_dev


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


class QfcSniper:
    def __init__(self):
        self.client = ExchangeClient()
        self.last_px = None
        self.last_ret = 0.0
        self.sigma2 = 0.0
        self.obs_count = 0
        self.prints_hist = deque(maxlen=50)
        self.open_pos = None     # {side, qty, entry_px, entry_ts, entry_obs_n}
        self.last_evt_ts = 0.0
        self.stats = {"signals": 0, "entries": 0, "wins": 0, "losses": 0, "stops": 0, "stales": 0}
        self.halted = False

    def _latest_qfc_from_state(self, state):
        """Find QFC's newest trade price in a state frame, if any."""
        latest = None
        latest_key = (-1, -1)
        for t in (state.get("recent_trades") or state.get("trades") or []):
            if t.get("symbol") != SYMBOL: continue
            px = _num(t.get("price"))
            if px <= 0: continue
            tick = _num(t.get("tick"))
            ts = _num(t.get("timestamp"))
            key = (tick, ts)
            if key > latest_key:
                latest_key = key; latest = px
        return latest

    def _ingest(self, state):
        px = self._latest_qfc_from_state(state)
        if px is None: return False
        if self.last_px is None:
            self.last_px = px
            self.prints_hist.append(px)
            return False
        if px == self.last_px:
            return False   # no new observation (same batch or stale)
        r = (px - self.last_px) / self.last_px
        self.last_ret = r
        a = SIGMA_EWMA_ALPHA
        self.sigma2 = (1 - a) * self.sigma2 + a * r * r
        self.obs_count += 1
        self.last_px = px
        self.prints_hist.append(px)
        return True

    def _maybe_exit(self, book, now):
        if self.open_pos is None: return
        bb, ba = _best(book)
        if not bb or not ba: return
        mid = 0.5 * (bb + ba)
        side, qty, entry_px, entry_ts, entry_obs = self.open_pos["side"], self.open_pos["qty"], self.open_pos["entry_px"], self.open_pos["entry_ts"], self.open_pos["entry_obs_n"]
        obs_held = self.obs_count - entry_obs

        if side == "long":
            uret = (mid - entry_px) / entry_px if entry_px else 0.0
            flat_px = bb
        else:
            uret = (entry_px - mid) / entry_px if entry_px else 0.0
            flat_px = ba

        stale = (obs_held >= MAX_HOLD_OBS) or (now - entry_ts > MAX_HOLD_SEC)
        hit_stop = uret <= -HARD_STOP_PCT
        profit = uret > 0 and obs_held >= 1   # first new obs that shows a profit

        reason = None
        if hit_stop: reason = "STOP"; self.stats["stops"] += 1
        elif profit: reason = "WIN"; self.stats["wins"] += 1
        elif stale:
            reason = "STALE"; self.stats["stales"] += 1
            if uret < 0: self.stats["losses"] += 1
        if not reason: return

        max_dev = SANE_STOP_DEV if reason == "STOP" else SANE_MAX_DEV
        if not _sane(flat_px, self.prints_hist, max_dev):
            log.warning("EXIT deferred (%s outside ±%.0f%% median)", flat_px, max_dev * 100)
            return
        try:
            (self.client.sell if side == "long" else self.client.buy)(SYMBOL, flat_px, qty)
            log.info("EXIT %s qty=%s @ %s reason=%s uret=%.3f%% held=%dobs", side, qty, flat_px, reason, uret*100, obs_held)
        except Exception as exc:
            log.warning("exit failed: %s", exc); return
        self.open_pos = None
        self.last_evt_ts = now

    def _maybe_enter(self, book, cap, now, new_obs):
        if not new_obs: return                # only consider entry on fresh observation
        if self.open_pos is not None: return
        if now - self.last_evt_ts < MIN_RE_ENTRY_SEC: return
        if self.obs_count < MIN_OBS_FOR_SIG: return
        if self.sigma2 <= 0: return

        sigma = math.sqrt(self.sigma2)
        trigger = TRIGGER_Z * sigma
        go_long = self.last_ret < -trigger
        go_short = self.last_ret > trigger
        if not (go_long or go_short): return

        bb, ba = _best(book)
        if not bb or not ba: return
        mid = 0.5 * (bb + ba)
        qty = round((SIZE_PCT * cap) / mid, 4)
        if qty <= 0: return

        exec_px = ba if go_long else bb
        if not _sane(exec_px, self.prints_hist, SANE_MAX_DEV): return

        side = "long" if go_long else "short"
        self.stats["signals"] += 1
        try:
            (self.client.buy if go_long else self.client.sell)(SYMBOL, exec_px, qty)
        except Exception as exc:
            log.warning("entry failed: %s", exc); return
        self.stats["entries"] += 1
        self.open_pos = {
            "side": side, "qty": qty, "entry_px": exec_px,
            "entry_ts": now, "entry_obs_n": self.obs_count,
        }
        self.last_evt_ts = now
        z = self.last_ret / sigma
        log.info("ENTER %s qty=%s @ %s r=%.3f%% z=%.2f sigma=%.3f%%", side, qty, exec_px, self.last_ret*100, z, sigma*100)

    def flatten_all(self):
        try: self.client.cancel_all()
        except Exception: pass
        self.open_pos = None

    def run(self):
        log.info("qfc_sniper online | symbol=%s | TRIGGER_Z=%.1f | SIZE=%.1f%% | STOP=%.1f%% | MAX_HOLD=%dobs/%ds",
                 SYMBOL, TRIGGER_Z, SIZE_PCT*100, HARD_STOP_PCT*100, MAX_HOLD_OBS, int(MAX_HOLD_SEC))
        last_team = 0.0; team = {}; last_status = 0.0; iters = 0
        for state in self.client.stream_state():
            try:
                now = time.monotonic()
                iters += 1
                if iters == 1:
                    log.info("first state | keys=%s | comp=%s", sorted((state or {}).keys()), state.get("competition_state"))

                comp = state.get("competition_state")
                live = (comp == "live")

                # Ingest QFC prints in every state (including pre_open) so sigma is
                # already warm when the market flips to live — avoids burning the
                # first ~4min of the session on warmup.
                new_obs = self._ingest(state)

                if not live:
                    if not self.halted: self.flatten_all(); self.halted = True
                    # status heartbeat still fires below
                    if now - last_status > 15.0:
                        sigma_pct = math.sqrt(self.sigma2) * 100 if self.sigma2 > 0 else 0
                        log.info("HB(%s) iter=%d obs=%d px=%s sigma=%.2f%% (warming)",
                                 comp, iters, self.obs_count, self.last_px, sigma_pct)
                        last_status = now
                    continue
                self.halted = False

                if now - last_team > 1.0:
                    try: team = self.client.get_team_state() or {}
                    except Exception as exc: log.warning("get_team_state: %s", exc)
                    last_team = now

                eq = _team_equity(team)
                if eq and eq < BAILOUT_HALT_EQUITY:
                    log.error("BAILOUT: equity %.0f", eq); self.flatten_all(); time.sleep(1.0); continue

                cap = _bot_capital(team, BOT_ID) or 100_000.0
                books = state.get("book") or state.get("books") or {}
                book = books.get(SYMBOL) or {}

                if book: self._maybe_exit(book, now)
                if book: self._maybe_enter(book, cap, now, new_obs)

                if now - last_status > 15.0:
                    s = self.stats
                    n = s["wins"] + s["losses"] + s["stops"]
                    wr = s["wins"] / n * 100 if n else 0
                    sigma_pct = math.sqrt(self.sigma2) * 100 if self.sigma2 > 0 else 0
                    log.info("HB iter=%d eq=%.0f cap=%.0f obs=%d px=%s sigma=%.2f%% signals=%d entries=%d wr=%.0f%% open=%s",
                             iters, eq, cap, self.obs_count, self.last_px, sigma_pct,
                             s["signals"], s["entries"], wr,
                             (self.open_pos["side"], self.open_pos["qty"]) if self.open_pos else None)
                    last_status = now
            except Exception as exc:
                log.exception("loop error: %s", exc)
                time.sleep(0.5)


def _supervised():
    """Outer auto-restart loop. Crashes in run() relaunch a fresh instance."""
    backoff = 1.0
    while True:
        try:
            QfcSniper().run()
            log.warning("run() returned cleanly — restarting in %.1fs", backoff)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.exception("FATAL in run(): %s — restarting in %.1fs", exc, backoff)
        time.sleep(backoff)
        backoff = min(60.0, backoff * 2)


if __name__ == "__main__":
    _supervised()
