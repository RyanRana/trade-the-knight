"""Pattern & trend analysis over data/*.jsonl captures.

Complements analyze_edges.py. Focused on:
  1. Trend vs mean-reversion regime (variance ratio by lag).
  2. Volatility clustering (autocorr of |returns|).
  3. Signal→price lead-lag (does the public series lead the mid by N snapshots?).
  4. Regime-shift detection (z-score jumps in mid).
  5. Spread dynamics around large moves.
  6. Cross-symbol move synchrony.
  7. Intraday-style drift by snapshot bucket.
"""

from __future__ import annotations
import json, math, statistics, collections, os

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")


def load_jsonl(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def banner(s):
    print("\n" + "=" * 78)
    print(s)
    print("=" * 78)


def best_bid_ask(sb):
    bids = sb.get("bids") or {}
    asks = sb.get("asks") or {}
    if not bids or not asks:
        return None, None
    try:
        bb = max(float(k) for k in bids)
        ba = min(float(k) for k in asks)
    except Exception:
        return None, None
    return bb, ba


def corr(xs, ys):
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs, ys = xs[:n], ys[:n]
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in xs))
    dy = math.sqrt(sum((v - my) ** 2 for v in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def autocorr(x, lag=1):
    n = len(x)
    if n <= lag + 1:
        return None
    m = sum(x) / n
    num = sum((x[i] - m) * (x[i - lag] - m) for i in range(lag, n))
    den = sum((v - m) ** 2 for v in x)
    return num / den if den > 0 else None


# --- load ---
book = load_jsonl(os.path.join(DATA, "book.jsonl"))
ts = load_jsonl(os.path.join(DATA, "timeseries.jsonl"))
trades = load_jsonl(os.path.join(DATA, "trades.jsonl"))

mids = collections.defaultdict(list)     # sym -> [(idx, mid, spread_bps)]
for idx, snap in enumerate(book):
    for sym, sb in (snap.get("payload") or {}).items():
        bb, ba = best_bid_ask(sb)
        if bb and ba and ba > bb:
            mid = 0.5 * (bb + ba)
            mids[sym].append((idx, mid, 10_000 * (ba - bb) / mid))

series = collections.defaultdict(list)   # sig_name -> [(idx, value)]
for idx, snap in enumerate(ts):
    for s in snap.get("payload") or []:
        v = s.get("latest_value")
        if v is None:
            continue
        series[s["name"]].append((idx, float(v)))

print(f"book snaps: {len(book)}   ts snaps: {len(ts)}   trade snaps: {len(trades)}")
print(f"symbols: {sorted(mids.keys())}")


# ---------- 1. Variance ratio test (trend vs mean-reversion) ----------
banner("Variance-ratio(q) — <1 mean-reverting, ≈1 random walk, >1 trending")

def variance_ratio(prices, q):
    rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices)) if prices[i - 1] > 0]
    n = len(rets)
    if n < q * 4:
        return None
    mu = sum(rets) / n
    var_1 = sum((r - mu) ** 2 for r in rets) / (n - 1)
    q_sums = [sum(rets[i:i + q]) for i in range(0, n - q + 1)]
    mu_q = sum(q_sums) / len(q_sums)
    var_q = sum((s - mu_q) ** 2 for s in q_sums) / (len(q_sums) - 1)
    return var_q / (q * var_1) if var_1 > 0 else None


print(f"{'sym':8s}{'VR(2)':>8s}{'VR(5)':>8s}{'VR(10)':>8s}{'verdict':>28s}")
for sym in sorted(mids.keys()):
    px = [m for _, m, _ in mids[sym]]
    vr2 = variance_ratio(px, 2)
    vr5 = variance_ratio(px, 5)
    vr10 = variance_ratio(px, 10)

    def fmt(x):
        return f"{x:.2f}" if x is not None else "--"

    def judge(v):
        if v is None:
            return ""
        if v < 0.6:
            return "mean-reverting"
        if v > 1.4:
            return "trending"
        return "random walk"

    print(f"{sym:8s}{fmt(vr2):>8s}{fmt(vr5):>8s}{fmt(vr10):>8s}{judge(vr5):>28s}")


# ---------- 2. Volatility clustering (autocorr of |return|) ----------
banner("Volatility clustering — autocorr of |return|. >0.2 means bursty vol")
print(f"{'sym':8s}{'n':>5s}{'ρ1(|r|)':>12s}{'ρ3(|r|)':>12s}{'ρ5(|r|)':>12s}")
for sym in sorted(mids.keys()):
    px = [m for _, m, _ in mids[sym]]
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if px[i - 1] > 0]
    absr = [abs(r) for r in rets]
    a1 = autocorr(absr, 1); a3 = autocorr(absr, 3); a5 = autocorr(absr, 5)
    f = lambda v: f"{v:.2f}" if v is not None else "--"
    print(f"{sym:8s}{len(absr):>5d}{f(a1):>12s}{f(a3):>12s}{f(a5):>12s}")


# ---------- 3. Signal lead-lag: corr(Δmid_t, Δsignal_{t-k}) at k=0..3 ----------
banner("Signal → mid lead-lag. Max corr @ k>0 means signal LEADS price by k snaps")
print(f"{'signal':18s}{'sym':8s}{'n':>5s}{'k=0':>8s}{'k=1':>8s}{'k=2':>8s}{'k=3':>8s}{'best_k':>8s}")


def align_by_idx(a_pairs, b_pairs):
    a = dict(a_pairs); b = dict(b_pairs)
    keys = sorted(set(a) & set(b))
    return [a[k] for k in keys], [b[k] for k in keys]


for sig_name in sorted(series.keys()):
    if sig_name == "ior_rate":
        continue
    for sym in sorted(mids.keys()):
        mid_pairs = [(idx, m) for idx, m, _ in mids[sym]]
        sig_pairs = series[sig_name]
        mm, ss = align_by_idx(mid_pairs, sig_pairs)
        if len(mm) < 10:
            continue
        dmm = [mm[i] - mm[i - 1] for i in range(1, len(mm))]
        dss = [ss[i] - ss[i - 1] for i in range(1, len(ss))]
        results = {}
        for k in range(0, 4):
            if k >= len(dmm):
                continue
            c = corr(dmm[k:], dss[: len(dmm) - k])
            if c is not None:
                results[k] = c
        if not results:
            continue
        best_k, best_c = max(results.items(), key=lambda kv: abs(kv[1]))
        if abs(best_c) < 0.3:
            continue

        def f(v):
            return f"{v:+.2f}" if v is not None else "  -- "

        print(f"{sig_name:18s}{sym:8s}{len(dmm):>5d}"
              f"{f(results.get(0)):>8s}{f(results.get(1)):>8s}"
              f"{f(results.get(2)):>8s}{f(results.get(3)):>8s}"
              f"{best_k:>8d}")


# ---------- 4. Regime shifts: |Δmid| > 3σ ----------
banner("Regime shifts — snapshots where |Δmid| > 3σ of its prior history")
for sym in sorted(mids.keys()):
    rows = mids[sym]
    if len(rows) < 15:
        continue
    jumps = []
    past = []
    for i, (idx, m, _) in enumerate(rows):
        if len(past) >= 10:
            m0 = past[-1]
            rets = [math.log(past[j] / past[j - 1]) for j in range(1, len(past)) if past[j - 1] > 0]
            if len(rets) >= 5:
                mu = sum(rets) / len(rets)
                sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
                if sd > 0 and m0 > 0:
                    r = math.log(m / m0)
                    if abs(r - mu) > 3 * sd:
                        jumps.append((idx, m0, m, (r - mu) / sd))
        past.append(m)
    if jumps:
        print(f"  {sym}: {len(jumps)} shift(s) — sample: ", end="")
        for (idx, m0, m1, z) in jumps[:3]:
            pct = 100 * (m1 - m0) / m0
            print(f"[snap {idx}: {m0:.2f}→{m1:.2f} ({pct:+.2f}%, z={z:+.1f})] ", end="")
        print()


# ---------- 5. Spread regime: does spread widen before or after big moves? ----------
banner("Spread vs |Δmid|: corr. Positive = wider-spread windows see bigger moves")
print(f"{'sym':8s}{'n':>5s}{'corr(spr, |Δmid|)':>22s}{'corr(spr_{t-1}, |Δmid_t|)':>28s}")
for sym in sorted(mids.keys()):
    rows = mids[sym]
    if len(rows) < 5:
        continue
    spr = [s for _, _, s in rows]
    px = [m for _, m, _ in rows]
    dmid = [abs(px[i] - px[i - 1]) for i in range(1, len(px))]
    c_now = corr(spr[1:], dmid)
    c_prev = corr(spr[:-1], dmid)
    f = lambda v: f"{v:+.2f}" if v is not None else " -- "
    print(f"{sym:8s}{len(dmid):>5d}{f(c_now):>22s}{f(c_prev):>28s}")


# ---------- 6. Cross-symbol move synchrony (hit rate of same-direction ticks) ----------
banner("Cross-symbol synchrony — fraction of snaps where both tick the same direction")
# Build per-symbol return series aligned by snap idx
ret_by_sym = {}
for sym, rows in mids.items():
    d = {}
    prev = None
    prev_idx = None
    for idx, m, _ in rows:
        if prev is not None and prev > 0 and idx == prev_idx + 1:
            d[idx] = 1 if m > prev else (-1 if m < prev else 0)
        prev, prev_idx = m, idx
    ret_by_sym[sym] = d

syms = sorted(ret_by_sym.keys())
print(f"{'pair':20s}{'n':>5s}{'same-dir%':>12s}{'bias':>8s}")
for i in range(len(syms)):
    for j in range(i + 1, len(syms)):
        a, b = syms[i], syms[j]
        keys = sorted(set(ret_by_sym[a]) & set(ret_by_sym[b]))
        keys = [k for k in keys if ret_by_sym[a][k] != 0 and ret_by_sym[b][k] != 0]
        if len(keys) < 10:
            continue
        same = sum(1 for k in keys if ret_by_sym[a][k] == ret_by_sym[b][k])
        pct = same / len(keys)
        if abs(pct - 0.5) < 0.15:
            continue
        bias = "+" if pct > 0.5 else "-"
        print(f"{a+'/'+b:20s}{len(keys):>5d}{100*pct:>11.1f}%{bias:>8s}")


# ---------- 7. Intraday-style drift: split snaps into thirds, compare mean returns ----------
banner("Drift by snapshot bucket (early/mid/late thirds of the capture)")
print(f"{'sym':8s}{'ret_early_bps':>16s}{'ret_mid_bps':>14s}{'ret_late_bps':>16s}")
for sym in sorted(mids.keys()):
    px = [m for _, m, _ in mids[sym]]
    n = len(px)
    if n < 12:
        continue
    a, b = n // 3, 2 * n // 3

    def seg_ret(lo, hi):
        if hi - lo < 2 or px[lo] <= 0:
            return None
        return 10_000 * (px[hi - 1] - px[lo]) / px[lo]

    r1, r2, r3 = seg_ret(0, a), seg_ret(a, b), seg_ret(b, n)
    f = lambda v: f"{v:+.0f}" if v is not None else "--"
    print(f"{sym:8s}{f(r1):>16s}{f(r2):>14s}{f(r3):>16s}")


# ---------- 8. Spread band: typical & extreme ----------
banner("Spread distribution (bps) — what's 'wide' per symbol")
print(f"{'sym':8s}{'n':>5s}{'p10':>8s}{'p50':>8s}{'p90':>8s}{'p99':>8s}")
for sym in sorted(mids.keys()):
    spr = sorted([s for _, _, s in mids[sym]])
    if len(spr) < 5:
        continue

    def pct(p):
        k = max(0, min(len(spr) - 1, int(p / 100 * (len(spr) - 1))))
        return spr[k]

    print(f"{sym:8s}{len(spr):>5d}{pct(10):>8.0f}{pct(50):>8.0f}{pct(90):>8.0f}{pct(99):>8.0f}")
