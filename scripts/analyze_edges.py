"""Quick edge-analysis over the JSONL captures in data/.

Reports:
- Per-symbol spread, mid drift, realized vol, autocorrelation (mean-revert vs momentum)
- Trade clustering and stale-price windows
- Order-book imbalance vs next-move correlation (on snapshots we have)
- Pairwise mid-price correlations (pairs-trading candidates)
- Timeseries signal vs mid correlation (lead-lag scan)
- Prediction-markets check
"""

from __future__ import annotations
import json, math, statistics, collections, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def load_jsonl(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def p(label):
    print("\n" + "=" * 72)
    print(label)
    print("=" * 72)


# --- Load ---
ts = load_jsonl(os.path.join(DATA, "timeseries.jsonl"))
book = load_jsonl(os.path.join(DATA, "book.jsonl"))
trades = load_jsonl(os.path.join(DATA, "trades.jsonl"))
tape = load_jsonl(os.path.join(DATA, "tape.jsonl"))
sim = load_jsonl(os.path.join(DATA, "sim_trades.jsonl"))

print(f"snapshots: ts={len(ts)} book={len(book)} trades={len(trades)} tape={len(tape)} sim={len(sim)}")


# ---------- 1. Mid-price series from book snapshots ----------
def best_bid_ask(symbol_book):
    bids = symbol_book.get("bids") or {}
    asks = symbol_book.get("asks") or {}
    if not bids or not asks:
        return None, None
    try:
        bb = max(float(x) for x in bids.keys())
        ba = min(float(x) for x in asks.keys())
    except Exception:
        return None, None
    return bb, ba


mids = collections.defaultdict(list)      # symbol -> [(t, mid)]
spreads = collections.defaultdict(list)   # symbol -> [rel spread bps]
imbalances = collections.defaultdict(list)  # symbol -> [(t, imb, mid)]

for snap in book:
    t = snap["t"]
    payload = snap.get("payload") or {}
    for sym, sb in payload.items():
        bb, ba = best_bid_ask(sb)
        if bb is None or ba is None or ba <= bb:
            continue
        mid = 0.5 * (bb + ba)
        spr = ba - bb
        mids[sym].append((t, mid))
        spreads[sym].append(10_000 * spr / mid)
        # L1 depth imbalance
        try:
            bq = sum(float(o["quantity"]) for o in sb["bids"][str(bb) if str(bb) in sb["bids"] else f"{bb:.4f}"])
        except Exception:
            # fall back: sum all level quantities (by reading string key literally)
            bq = 0.0
            for k, orders in sb["bids"].items():
                if abs(float(k) - bb) < 1e-6:
                    bq += sum(float(o["quantity"]) for o in orders)
        try:
            aq = sum(float(o["quantity"]) for o in sb["asks"][str(ba) if str(ba) in sb["asks"] else f"{ba:.4f}"])
        except Exception:
            aq = 0.0
            for k, orders in sb["asks"].items():
                if abs(float(k) - ba) < 1e-6:
                    aq += sum(float(o["quantity"]) for o in orders)
        if bq + aq > 0:
            imb = (bq - aq) / (bq + aq)
            imbalances[sym].append((t, imb, mid))


p("Per-symbol snapshot stats")
print(f"{'sym':8s}{'n':>5s}{'avg_spr_bps':>14s}{'med_spr_bps':>14s}{'mid_mean':>12s}{'mid_std':>12s}{'ret_std_bps':>14s}")
symbol_stats = {}
for sym, arr in sorted(mids.items()):
    arr.sort()
    values = [m for _, m in arr]
    rets = []
    for i in range(1, len(values)):
        if values[i-1] > 0:
            rets.append(10_000 * (values[i] - values[i-1]) / values[i-1])
    sp = spreads[sym]
    if not values:
        continue
    sm = statistics.mean(values)
    ss = statistics.pstdev(values) if len(values) > 1 else 0.0
    rs = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    avg_sp = statistics.mean(sp) if sp else 0.0
    med_sp = statistics.median(sp) if sp else 0.0
    symbol_stats[sym] = {"mids": values, "rets": rets, "spread_bps": avg_sp}
    print(f"{sym:8s}{len(values):>5d}{avg_sp:>14.1f}{med_sp:>14.1f}{sm:>12.2f}{ss:>12.3f}{rs:>14.1f}")


# ---------- 2. Autocorrelation of 1-lag returns (mean-revert vs momentum) ----------
def autocorr1(x):
    n = len(x)
    if n < 3:
        return None
    m = sum(x) / n
    num = sum((x[i] - m) * (x[i-1] - m) for i in range(1, n))
    den = sum((v - m) ** 2 for v in x)
    return num / den if den > 0 else None


p("1-lag return autocorrelation (negative = mean reverting, positive = momentum)")
print(f"{'sym':8s}{'n':>5s}{'rho1':>10s}")
for sym, st in sorted(symbol_stats.items()):
    rho = autocorr1(st["rets"])
    print(f"{sym:8s}{len(st['rets']):>5d}{(f'{rho:.3f}' if rho is not None else 'n/a'):>10s}")


# ---------- 3. Pairwise mid correlations ----------
p("Pairwise mid correlations (from co-aligned snapshots)")


def corr(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in xs))
    dy = math.sqrt(sum((v - my) ** 2 for v in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


# align by snapshot index (book snapshots are ordered)
aligned = collections.defaultdict(list)  # sym -> list of mids, one per snapshot where symbol existed
snap_count = len(book)
for idx, snap in enumerate(book):
    for sym, sb in snap.get("payload", {}).items():
        bb, ba = best_bid_ask(sb)
        if bb and ba and ba > bb:
            aligned[sym].append((idx, 0.5 * (bb + ba)))

# dense align pairs
syms = sorted(aligned.keys())
maps = {s: dict(v) for s, v in aligned.items()}
print(f"{'pair':20s}{'n':>5s}{'corr(mid)':>12s}{'corr(ret)':>12s}")
for i in range(len(syms)):
    for j in range(i + 1, len(syms)):
        a, b = syms[i], syms[j]
        common = sorted(set(maps[a]) & set(maps[b]))
        if len(common) < 5:
            continue
        xa = [maps[a][k] for k in common]
        xb = [maps[b][k] for k in common]
        c_mid = corr(xa, xb)
        ra = [10_000 * (xa[k] - xa[k-1]) / xa[k-1] for k in range(1, len(xa)) if xa[k-1] > 0]
        rb = [10_000 * (xb[k] - xb[k-1]) / xb[k-1] for k in range(1, len(xb)) if xb[k-1] > 0]
        m = min(len(ra), len(rb))
        c_ret = corr(ra[:m], rb[:m])
        print(f"{a+'/'+b:20s}{len(common):>5d}"
              f"{(f'{c_mid:.3f}' if c_mid is not None else 'n/a'):>12s}"
              f"{(f'{c_ret:.3f}' if c_ret is not None else 'n/a'):>12s}")


# ---------- 4. Order-book imbalance vs next-tick return ----------
p("Book-imbalance predictive power (corr(imb_t, ret_{t→t+1}))")
print(f"{'sym':8s}{'n':>5s}{'corr(imb, fwd_ret)':>22s}")
for sym, rows in sorted(imbalances.items()):
    rows.sort()
    n = len(rows)
    if n < 5:
        continue
    imbs = [r[1] for r in rows[:-1]]
    rets = []
    for i in range(1, n):
        p0 = rows[i-1][2]
        p1 = rows[i][2]
        if p0 > 0:
            rets.append((p1 - p0) / p0)
    c = corr(imbs, rets)
    print(f"{sym:8s}{n-1:>5d}{(f'{c:.3f}' if c is not None else 'n/a'):>22s}")


# ---------- 5. Timeseries signals vs mid (lead-lag at snapshot granularity) ----------
p("Public timeseries → symbol lead-lag (corr of same-time snapshots)")

ts_values = collections.defaultdict(list)  # name -> [(t, val)]
for snap in ts:
    for s in snap.get("payload", []):
        if s.get("latest_value") is None:
            continue
        ts_values[s["name"]].append((snap["t"], float(s["latest_value"])))

# find nearest ts value for each book snapshot time
def nearest(series_sorted, t):
    # series_sorted by time
    lo, hi = 0, len(series_sorted) - 1
    if not series_sorted:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if series_sorted[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    return series_sorted[lo][1]

ts_sorted = {k: sorted(v) for k, v in ts_values.items()}
print(f"{'signal':20s}{'sym':8s}{'n':>5s}{'corr(mid,sig)':>16s}{'corr(dmid,dsig)':>18s}")
for sig_name, sig_series in sorted(ts_sorted.items()):
    if sig_name == "ior_rate":
        continue
    for sym, pairs in sorted(mids.items()):
        xs, ys = [], []
        for (t, m) in pairs:
            v = nearest(sig_series, t)
            if v is not None:
                xs.append(m)
                ys.append(v)
        if len(xs) < 5:
            continue
        c = corr(xs, ys)
        dx = [xs[k] - xs[k-1] for k in range(1, len(xs))]
        dy = [ys[k] - ys[k-1] for k in range(1, len(ys))]
        cd = corr(dx, dy)
        if c is None:
            continue
        if abs(c) >= 0.5 or (cd is not None and abs(cd) >= 0.4):
            print(f"{sig_name:20s}{sym:8s}{len(xs):>5d}"
                  f"{c:>16.3f}"
                  f"{(cd if cd is not None else float('nan')):>18.3f}")


# ---------- 6. Trades summary ----------
p("Public trades summary")
# Last trades snapshot = richest
last_trades = trades[-1]["payload"] if trades else []
by_sym = collections.defaultdict(list)
for tr in last_trades:
    by_sym[tr["symbol"]].append(tr)
print(f"{'sym':8s}{'n_trades':>10s}{'px_min':>10s}{'px_max':>10s}{'px_last':>10s}")
for sym, rows in sorted(by_sym.items()):
    px = [float(r["price"]) for r in rows]
    print(f"{sym:8s}{len(px):>10d}{min(px):>10.2f}{max(px):>10.2f}{px[-1]:>10.2f}")


# ---------- 7. Bot quoting edge from sim_trades.jsonl ----------
p("MM quoting: average spread captured by symbol (from sim_trades 'quote' rows)")
mm_spreads = collections.defaultdict(list)
for row in sim:
    if row.get("bot") == "MM" and row.get("action") == "quote":
        sym = row.get("symbol")
        sp = row.get("spread")
        mid = row.get("mid")
        if sym and sp and mid:
            mm_spreads[sym].append(10_000 * sp / mid)
print(f"{'sym':8s}{'n':>5s}{'avg_q_spread_bps':>20s}{'med_q_spread_bps':>20s}")
for sym, arr in sorted(mm_spreads.items()):
    print(f"{sym:8s}{len(arr):>5d}{statistics.mean(arr):>20.1f}{statistics.median(arr):>20.1f}")


# ---------- 8. Stale-book detection (book prints without trade between) ----------
p("Trade intensity (trades per book snapshot)")
# rough: unique (sym, price, executed_at) over all trade snapshots
seen = set()
per_sym = collections.Counter()
for snap in trades:
    for tr in snap.get("payload", []):
        k = (tr["symbol"], tr["price"], tr["executed_at"])
        if k in seen:
            continue
        seen.add(k)
        per_sym[tr["symbol"]] += 1
print(f"{'sym':8s}{'unique_trades':>14s}{'book_snaps':>12s}{'trades/snap':>14s}")
for sym in sorted(mids.keys()):
    n_snaps = len(mids[sym])
    n_tr = per_sym[sym]
    rate = n_tr / n_snaps if n_snaps else 0
    print(f"{sym:8s}{n_tr:>14d}{n_snaps:>12d}{rate:>14.3f}")
