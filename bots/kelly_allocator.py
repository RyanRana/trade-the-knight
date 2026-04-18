"""
kelly_allocator.py — meta-strategy, bot-capital sizing reporter (§3.20).

This bot does not trade directly. Every REPORT_EVERY_SEC it snapshots each
sibling bot's per-bot realized PnL + capital via get_team_state(), estimates
μ (mean return) and σ² (variance of returns) from its own rolling window,
and logs the half-Kelly capital fraction per bot:

    f* = (μ - r) / σ²      (clipped to [0, 0.25])
    half_kelly = 0.5 * f*

You (the operator) read the log and reallocate bot capital through the
dashboard. The bot keeps all its own capital idle in RUD so it earns IOR too.
"""

import os, time, logging
from collections import defaultdict, deque
import numpy as np
from knight_trader import ExchangeClient

log = logging.getLogger("kelly_allocator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
BOT_ID = os.environ.get("BOT_ID","")

WINDOW = 30
REPORT_EVERY_SEC = 60.0
HARD_CAP = 0.25


def _num(x,d=0.0):
    try: return float(x)
    except: return d


def _ior(client):
    for n in ("ior_rate","fed_rate"):
        try:
            pts = client.get_timeseries(n, limit=1) or []
            if pts:
                v = pts[-1].get("v") if isinstance(pts[-1],dict) else pts[-1]
                if _num(v)>0: return _num(v)
        except: continue
    return 0.0


def _bots_view(ts):
    bs = ts.get("bots") or ts.get("portfolio",{}).get("bots") or {}
    if isinstance(bs, dict): return [(k, v) for k,v in bs.items()]
    return [((b.get("id") or b.get("bot_id")), b) for b in bs]


def run():
    client = ExchangeClient()
    equity_hist = defaultdict(lambda: deque(maxlen=WINDOW+5))
    last_report = 0.0
    while True:
        try:
            ts = client.get_team_state() or {}
            r = _ior(client)
            for bot_id, b in _bots_view(ts):
                if not bot_id: continue
                cap = _num(b.get("capital") or b.get("allocated_capital"))
                pnl = _num(b.get("realized_pnl") or b.get("pnl"))
                # Compute a per-tick return approximation.
                value = cap + pnl
                equity_hist[bot_id].append(value)

            now = time.monotonic()
            if now - last_report >= REPORT_EVERY_SEC:
                last_report = now
                lines = []
                for bot_id, arr in equity_hist.items():
                    if len(arr) < 5: continue
                    a = np.array(arr)
                    rets = np.diff(a) / (a[:-1] + 1e-9)
                    mu = rets.mean()
                    var = rets.var() or 1e-9
                    f = (mu - r) / var
                    f = max(0.0, min(HARD_CAP, f))
                    half = 0.5 * f
                    lines.append((bot_id, mu, var, f, half))
                lines.sort(key=lambda x: -x[4])
                log.info("KELLY ALLOCATION REPORT (half-Kelly, sorted)")
                for bid, mu, var, f, half in lines:
                    log.info("  bot=%s mu=%.5f var=%.5f full_f*=%.3f half_kelly=%.3f", bid, mu, var, f, half)
            time.sleep(5)
        except Exception as e:
            log.exception("loop: %s", e); time.sleep(5)


if __name__ == "__main__":
    run()
