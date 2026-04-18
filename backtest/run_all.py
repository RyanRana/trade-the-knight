"""End-to-end driver:

    python -m backtest.run_all                 # baseline + tune all + emit suite
    python -m backtest.run_all --trials 120    # heavier search
    python -m backtest.run_all --baseline-only # just score current bots

Outputs:
    published_bots_tuned/<bot>.py   — improved copies
    backtest_out/leaderboard.json   — ranked results with overrides + metrics
    backtest_out/<bot>_trials.json  — per-trial detail (from tune.py)
"""
from __future__ import annotations
import os, json, argparse, time as _real_time

from . import replay as _replay
from . import score as _score
from . import tune as _tune
from . import params as _params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISHED_DIR = os.path.join(ROOT, "published_bots")
TUNED_DIR = os.path.join(ROOT, "published_bots_tuned")
OUT_DIR = os.path.join(ROOT, "backtest_out")
os.makedirs(TUNED_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)


def baseline_table(events, tick_every_n_events=40):
    rows = []
    for bot in _params.all_bots():
        path = os.path.join(PUBLISHED_DIR, bot + ".py")
        if not os.path.exists(path):
            continue
        r = _score.run_bot(path, overrides=None, events=events,
                           tick_every_n_events=tick_every_n_events)
        rows.append({
            "bot": bot,
            "net_pnl": r.get("net_pnl"),
            "score": r.get("score"),
            "fills": r.get("fills"),
            "ok": r.get("ok"),
            "error": r.get("error"),
            "symbols_touched": r.get("symbols_touched"),
            "final_equity": r.get("final_equity"),
            "realized_pnl": r.get("realized_pnl"),
            "max_gross_inventory": r.get("max_gross_inventory"),
        })
    return rows


def pretty_table(rows, title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    header = f"{'bot':25s} {'score':>10s} {'net_pnl':>10s} {'fills':>7s} {'syms':>5s}"
    print(header)
    print("-" * len(header))
    for r in rows:
        syms = len(r.get("symbols_touched") or [])
        print(f"{r['bot']:25s} {r.get('score',0):+10.0f} {r.get('net_pnl',0):+10.0f} "
              f"{r.get('fills',0) or 0:7d} {syms:5d}"
              + (f"  ERR: {r['error']}" if r.get('error') else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=60,
                    help="random-search trials per bot (default 60)")
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--tick-every", type=int, default=40,
                    help="events per emitted state snapshot (higher = faster; 40 ≈ 5k ticks)")
    ap.add_argument("--baseline-only", action="store_true",
                    help="score current published bots and exit (no tuning)")
    ap.add_argument("--bots", nargs="+", default=None,
                    help="subset of bots to run (default: all)")
    args = ap.parse_args()

    t0 = _real_time.monotonic()
    print(f"loading replay events from data/*.jsonl ...")
    events = list(_replay.merged_events())
    print(f"  {len(events)} events loaded, tick_every_n={args.tick_every}")

    bots = args.bots or _params.all_bots()

    print("\n[1/2] baseline scoring ...")
    base_rows = []
    for bot in bots:
        path = os.path.join(PUBLISHED_DIR, bot + ".py")
        if not os.path.exists(path):
            continue
        r = _score.run_bot(path, overrides=None, events=events,
                           tick_every_n_events=args.tick_every)
        base_rows.append({
            "bot": bot,
            "net_pnl": r.get("net_pnl"), "score": r.get("score"),
            "fills": r.get("fills"), "ok": r.get("ok"),
            "error": r.get("error"),
            "symbols_touched": r.get("symbols_touched"),
        })
    base_rows.sort(key=lambda r: -(r.get("score") or 0))
    pretty_table(base_rows, "BASELINE (current published_bots/)")

    if args.baseline_only:
        with open(os.path.join(OUT_DIR, "baseline.json"), "w") as f:
            json.dump(base_rows, f, indent=2, default=str)
        print(f"\nwrote {OUT_DIR}/baseline.json. elapsed {_real_time.monotonic()-t0:.1f}s")
        return

    print(f"\n[2/2] tuning {len(bots)} bots × {args.trials} trials ...")
    tuned_rows = []
    for bot in bots:
        try:
            r = _tune.tune_bot(bot, trials=args.trials, seed=args.seed,
                               events=events, tick_every_n_events=args.tick_every)
            print(f"  {bot:25s} base={r['baseline_score']:+10.0f} "
                  f"best={r['best_score']:+10.0f} Δ={r['improvement']:+10.0f} ({r['elapsed_sec']}s)")
            tuned_rows.append(r)
        except Exception as exc:
            print(f"  {bot:25s} TUNE ERROR: {exc}")
            tuned_rows.append({"bot": bot, "error": str(exc)})

    print("\nscoring tuned suite in published_bots_tuned/ ...")
    final_rows = []
    for bot in bots:
        path = os.path.join(TUNED_DIR, bot + ".py")
        if not os.path.exists(path):
            continue
        r = _score.run_bot(path, overrides=None, events=events,
                           tick_every_n_events=args.tick_every)
        final_rows.append({
            "bot": bot,
            "score": r.get("score"), "net_pnl": r.get("net_pnl"),
            "fills": r.get("fills"), "ok": r.get("ok"),
            "error": r.get("error"),
            "symbols_touched": r.get("symbols_touched"),
        })
    final_rows.sort(key=lambda r: -(r.get("score") or 0))
    pretty_table(final_rows, "TUNED (published_bots_tuned/) — final rankings")

    leaderboard = {
        "baseline": base_rows,
        "tuned": final_rows,
        "tune_summary": tuned_rows,
        "config": {
            "trials": args.trials, "seed": args.seed,
            "tick_every_n_events": args.tick_every,
            "events": len(events),
        },
    }
    with open(os.path.join(OUT_DIR, "leaderboard.json"), "w") as f:
        json.dump(leaderboard, f, indent=2, default=str)
    print(f"\nwrote {OUT_DIR}/leaderboard.json")
    print(f"tuned suite in {TUNED_DIR}/ — ready for upload")
    print(f"total elapsed: {_real_time.monotonic()-t0:.1f}s")


if __name__ == "__main__":
    main()
