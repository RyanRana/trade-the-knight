"""
paper_fleet.py — 4-bot paper trading fleet running on live public data.

Polls the public REST API at documented rate limits, runs the signal logic
for four strategies, and prints proposed trades. No real orders are placed
(that requires a BOT_ID issued by the competition dashboard).

Fleet (rev2 — sized for current thin-book regime):
  MM     — aggressive inside-quoting on every spot book, tight inventory cap,
           self-match guard so we don't cross our own resting order on 1bid/1ask books.
  HMR    — HILL mean-reverter: EMA-20 on HILL trade prints, fires BUY when
           price < EMA*0.98, SELL when price > EMA*1.02. HILL carries ~85% of
           observed volume so this is where real signal arrives.
  IOR    — risk-free carry tracker on ior_rate; establishes the hurdle rate
           every other strategy must beat.
  SCOUT  — first-mover detector. Flags new symbols appearing in /book and
           symbols that transition from dormant to active (new prints after
           silence). First on a freshly-seeded book is the highest-EV trade.
"""
import json
import os
import ssl
import sys
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl.create_default_context()


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


HERE = os.path.dirname(os.path.abspath(__file__))
load_env(os.path.join(HERE, "visualizer", ".env.local"))

DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)


def persist_raw(name, raw):
    path = os.path.join(DATA_DIR, f"{name}.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps({"t": time.time(), "payload": raw}, default=str) + "\n")


def log_sim(bot, action, **fields):
    row = {"t": time.time(), "bot": bot, "action": action, **fields}
    with open(os.path.join(DATA_DIR, "sim_trades.jsonl"), "a") as f:
        f.write(json.dumps(row, default=str) + "\n")


BASE = os.environ.get("EXCHANGE_BASE_URL", "https://tradetheknight.com").rstrip("/")
KEY = os.environ.get("EXCHANGE_API_KEY", "")
if not KEY:
    sys.exit("set EXCHANGE_API_KEY (or create visualizer/.env.local)")

CLR = {
    "MM":    "\033[36m",
    "HMR":   "\033[35m",
    "IOR":   "\033[32m",
    "SCOUT": "\033[33m",
    "SYS":   "\033[37m",
    "RST":   "\033[0m",
    "DIM":   "\033[2m",
}

CAPITAL = 100_000.0

# MM tuning — books are 1bid/1ask so quoting too tightly risks self-match.
MM_MIN_SPREAD     = 0.05
MM_INSIDE         = 0.05     # step inside NBBO (wider than before to avoid self-match)
MM_SIZE_PCT       = 0.008    # smaller per-quote size: thin books = fast adverse selection
MM_MAX_INV_PCT    = 0.02     # cap inventory per symbol

# HMR tuning — mean reversion on HILL only.
FOCUS_SYM         = "HILL"
HMR_EMA_SPAN      = 20
HMR_BUY_BAND      = 0.98
HMR_SELL_BAND     = 1.02
HMR_SIZE_PCT      = 0.05

# SCOUT tuning.
SCOUT_DORMANT_SEC = 120.0    # symbol "dormant" if no prints for this long

WINDOW = 100
TRADE_HIST = defaultdict(lambda: deque(maxlen=WINDOW))
LAST_PRINT_TS = {}             # symbol -> last observed trade wall-time
KNOWN_SYMS = set()             # symbols we've ever seen in /book
BOOK = {}
IOR = 0.0
HILL_EMA = None                # exponential moving average of HILL prints


def log(bot, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{CLR['DIM']}{ts}{CLR['RST']} {CLR[bot]}[{bot:5}]{CLR['RST']} {msg}", flush=True)


def get(path):
    req = urllib.request.Request(
        f"{BASE}/api/exchange/public/{path}",
        headers={
            "X-API-Key": KEY,
            "Accept": "application/json",
            "User-Agent": "paper-fleet/2.0 (knight-trader)",
        },
    )
    with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
        return json.loads(r.read())


def _fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def best(book):
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bp = [_fnum(k) for k in bids.keys() if _fnum(k) > 0]
    ap = [_fnum(k) for k in asks.keys() if _fnum(k) > 0]
    return (max(bp) if bp else None, min(ap) if ap else None)


def run_mm():
    hits = 0
    for sym, book in BOOK.items():
        if sym == "RUD":
            continue
        bb, ba = best(book)
        if not bb or not ba or bb >= ba:
            continue
        spread = ba - bb
        if spread < MM_MIN_SPREAD:
            continue
        mid = 0.5 * (bb + ba)
        bid_px = round(bb + MM_INSIDE, 4)
        ask_px = round(ba - MM_INSIDE, 4)
        # Self-match guard: if our inside-bid would cross our inside-ask, skip.
        if bid_px >= ask_px:
            log("MM", CLR["DIM"] + f"{sym:5} skip (self-match: {bid_px} >= {ask_px})" + CLR["RST"])
            continue
        qty = round((MM_SIZE_PCT * CAPITAL) / mid, 4)
        log("MM", f"{sym:5} quote {bid_px:.4f} / {ask_px:.4f} x {qty} (mid {mid:.4f}, spread {spread:.4f})")
        log_sim("MM", "quote", symbol=sym, bid=bid_px, ask=ask_px, qty=qty, mid=mid, spread=spread)
        hits += 1
    if not hits:
        log("MM", CLR["DIM"] + "no book meets spread threshold" + CLR["RST"])


def update_ema(new_price):
    """Classic EMA: alpha = 2/(N+1)."""
    global HILL_EMA
    alpha = 2.0 / (HMR_EMA_SPAN + 1.0)
    HILL_EMA = new_price if HILL_EMA is None else alpha * new_price + (1 - alpha) * HILL_EMA


def run_hmr():
    if FOCUS_SYM not in BOOK:
        log("HMR", CLR["DIM"] + f"{FOCUS_SYM} not listed" + CLR["RST"])
        return
    if HILL_EMA is None:
        log("HMR", CLR["DIM"] + f"{FOCUS_SYM} EMA not warm yet (0 prints)" + CLR["RST"])
        return
    bb, ba = best(BOOK[FOCUS_SYM])
    if not bb or not ba:
        log("HMR", CLR["DIM"] + f"{FOCUS_SYM} no book" + CLR["RST"])
        return
    mid = 0.5 * (bb + ba)
    ratio = mid / HILL_EMA
    size = round((HMR_SIZE_PCT * CAPITAL) / mid, 4)
    if ratio < HMR_BUY_BAND:
        log("HMR", f"{FOCUS_SYM} BUY {size} @ ~{ba:.4f}  mid/ema={ratio:.3f} (ema {HILL_EMA:.4f})")
        log_sim("HMR", "buy", symbol=FOCUS_SYM, price=ba, qty=size, ema=HILL_EMA, ratio=ratio)
    elif ratio > HMR_SELL_BAND:
        log("HMR", f"{FOCUS_SYM} SELL {size} @ ~{bb:.4f}  mid/ema={ratio:.3f} (ema {HILL_EMA:.4f})")
        log_sim("HMR", "sell", symbol=FOCUS_SYM, price=bb, qty=size, ema=HILL_EMA, ratio=ratio)
    else:
        log("HMR", CLR["DIM"] + f"{FOCUS_SYM} flat  mid {mid:.4f} ema {HILL_EMA:.4f} ratio {ratio:.3f}" + CLR["RST"])


def run_scout():
    now = time.time()
    book_syms = set(BOOK.keys()) - {"RUD"}
    # (1) Brand-new symbols in /book.
    new_syms = book_syms - KNOWN_SYMS
    for s in new_syms:
        log("SCOUT", f"NEW SYMBOL in /book: {s} — check tape for admin seeding")
        log_sim("SCOUT", "new_symbol", symbol=s)
    KNOWN_SYMS.update(book_syms)

    # (2) Dormant→active transitions.
    for sym, last in list(LAST_PRINT_TS.items()):
        if sym in book_syms and now - last < 5.0:
            # had a print within the last 5s after being silent
            pass

    # (3) Quiet symbols that might be about to wake up (book listed but no trades ever).
    silent = [s for s in book_syms if s not in LAST_PRINT_TS]
    if silent:
        log("SCOUT", CLR["DIM"] + f"silent (never traded): {silent}" + CLR["RST"])


def run_ior():
    if IOR <= 0:
        return
    daily = CAPITAL * IOR / 365
    log("IOR", f"ior_rate={IOR:.4%} → paper RUD {CAPITAL:,.0f} accrues ≈{daily:,.2f}/day (hurdle rate)")
    log_sim("IOR", "accrue", rate=IOR, daily=daily, capital=CAPITAL)


def safe_get(path, label):
    try:
        return get(path)
    except Exception as e:
        log("SYS", f"{label} fetch failed: {e}")
        return None


def ingest_trades(data):
    now = time.time()
    for t in data:
        sym = t.get("symbol")
        price = _fnum(t.get("price"))
        ts = _fnum(t.get("tick")) or _fnum(t.get("timestamp"))
        if not sym or price <= 0:
            continue
        TRADE_HIST[sym].append((ts, price))
        # dormant→active detection
        last = LAST_PRINT_TS.get(sym)
        if last is not None and now - last > SCOUT_DORMANT_SEC:
            log("SCOUT", f"WAKE: {sym} traded after {now-last:.0f}s silence @ {price}")
            log_sim("SCOUT", "wake", symbol=sym, price=price, silence_sec=now - last)
        LAST_PRINT_TS[sym] = now
        # HMR EMA update: HILL only
        if sym == FOCUS_SYM:
            update_ema(price)


def main():
    global IOR
    log("SYS", f"paper fleet v2 online — base={BASE}")
    log("SYS", "fleet = MM + HMR(HILL) + IOR + SCOUT   (paper only)")

    next_book = 0.0
    next_trades = 0.0
    next_ts = 0.0
    next_lb = 0.0

    while True:
        now = time.time()

        if now >= next_book:
            data = safe_get("book", "book")
            if isinstance(data, dict):
                persist_raw("book", data)
                BOOK.clear()
                BOOK.update(data)
                run_scout()
                run_mm()
                run_hmr()
            next_book = now + 31

        if now >= next_trades:
            data = safe_get("trades", "trades")
            if isinstance(data, list):
                persist_raw("trades", data)
                ingest_trades(data)
                nonempty = {s: len(v) for s, v in TRADE_HIST.items() if v}
                if nonempty:
                    log("SYS", CLR["DIM"] + f"prints: {nonempty}" + CLR["RST"])
            tape = safe_get("tape", "tape")
            if isinstance(tape, list):
                persist_raw("tape", tape)
            next_trades = now + 61

        if now >= next_ts:
            data = safe_get("timeseries", "timeseries")
            if isinstance(data, list):
                persist_raw("timeseries", data)
                for s in data:
                    if s.get("name") == "ior_rate":
                        IOR = _fnum(s.get("latest_value"))
                run_ior()
            next_ts = now + 31

        if now >= next_lb:
            data = safe_get("leaderboard", "leaderboard")
            if isinstance(data, list):
                persist_raw("leaderboard", data)
                if not data:
                    log("SYS", CLR["DIM"] + "leaderboard empty (first-mover window open)" + CLR["RST"])
                else:
                    top = sorted(data, key=lambda r: _fnum(r.get("equity") or r.get("total_equity")), reverse=True)[:3]
                    names = [f"{r.get('team') or r.get('name')}={_fnum(r.get('equity') or r.get('total_equity')):,.0f}" for r in top]
                    log("SYS", f"leaderboard top: {', '.join(names)}")
            next_lb = now + 31

        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log("SYS", "stopped")
