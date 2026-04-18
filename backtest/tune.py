"""Random search over a bot's tunable constants, "Karpathy auto" style.

Pick N random combinations of overrides from `params.TUNABLES[bot]`, backtest
each, keep the best. Then patch the winning constants into a copy of the bot
at `published_bots_tuned/<bot>.py`.

Usage (from project root):
    python -m backtest.tune spread_farmer --trials 80
    python -m backtest.tune --all --trials 60
"""
from __future__ import annotations
import os, sys, json, random, re, argparse, time as _real_time
from typing import Dict, Any, List, Tuple

from . import replay as _replay
from . import score as _score
from . import params as _params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISHED_DIR = os.path.join(ROOT, "published_bots")
TUNED_DIR = os.path.join(ROOT, "published_bots_tuned")
OUT_DIR = os.path.join(ROOT, "backtest_out")
os.makedirs(TUNED_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)


def _sample_overrides(grid: Dict[str, List[Any]], rng: random.Random) -> Dict[str, Any]:
    return {k: rng.choice(vs) for k, vs in grid.items()}


def _patch_bot_file(src_path: str, dst_path: str, overrides: Dict[str, Any],
                    header_note: str) -> None:
    """Rewrite the bot with overridden constants and a small tuning header."""
    with open(src_path) as f:
        src = f.read()

    for name, val in overrides.items():
        # Match "NAME = <rhs>" at line start (allowing spaces). Replace RHS up to
        # end-of-line or trailing inline comment. Only replace FIRST occurrence
        # so we don't touch the __main__ guard or variables inside functions.
        pattern = re.compile(rf"^({re.escape(name)}\s*=\s*)(.*?)(\s*#[^\n]*)?$", re.MULTILINE)
        # Put a single space before the inline comment if one existed, so we
        # don't collide the new value directly against "#".
        def _repl(m, v=val):
            trailing = m.group(3) or ""
            if trailing and not trailing.startswith((" ", "\t")):
                trailing = " " + trailing
            return f"{m.group(1)}{_lit(v)}{trailing}"
        replacement = _repl
        new_src, n = pattern.subn(replacement, src, count=1)
        if n == 0:
            # Constant not found at module level; skip silently.
            continue
        src = new_src

    banner = (
        '"""AUTO-TUNED by backtest/tune.py\n'
        f"{header_note}\n"
        'Generated copy — edit the source in published_bots/ and re-run tune.py instead.\n"""\n'
    )
    # Put the banner above the existing docstring/code.
    src = banner + src
    with open(dst_path, "w") as f:
        f.write(src)


def _lit(v: Any) -> str:
    """Safe literal repr for ints/floats/bools/strings — covers our grid types."""
    if isinstance(v, bool):
        return repr(v)
    if isinstance(v, int):
        return repr(v)
    if isinstance(v, float):
        # keep trailing ".0" for readability
        return repr(v)
    if isinstance(v, str):
        return repr(v)
    return repr(v)


def tune_bot(bot: str, trials: int, seed: int = 17,
             events: List[dict] | None = None,
             tick_every_n_events: int = 40) -> Dict[str, Any]:
    grid = _params.TUNABLES.get(bot)
    if not grid:
        raise KeyError(f"no tunable grid for {bot}")
    src_path = os.path.join(PUBLISHED_DIR, bot + ".py")
    if not os.path.exists(src_path):
        raise FileNotFoundError(src_path)

    if events is None:
        events = list(_replay.merged_events())

    rng = random.Random(seed)

    # Baseline (no overrides) first so we know if tuning actually helped.
    t0 = _real_time.monotonic()
    baseline = _score.run_bot(src_path, overrides=None, events=events,
                              tick_every_n_events=tick_every_n_events)
    baseline_score = baseline.get("score", float("-inf"))

    trials_log: List[Dict[str, Any]] = []
    best = {"score": baseline_score, "overrides": {}, "stats": baseline}

    for i in range(trials):
        ov = _sample_overrides(grid, rng)
        res = _score.run_bot(src_path, overrides=ov, events=events,
                             tick_every_n_events=tick_every_n_events)
        trials_log.append({
            "trial": i, "overrides": ov,
            "score": res.get("score"), "net_pnl": res.get("net_pnl"),
            "fills": res.get("fills"), "ok": res.get("ok"),
            "error": res.get("error"),
        })
        if res.get("score") is not None and res["score"] > best["score"]:
            best = {"score": res["score"], "overrides": ov, "stats": res}

    elapsed = _real_time.monotonic() - t0

    improved = best["score"] - baseline_score
    note = (
        f"bot: {bot}\n"
        f"trials: {trials}\n"
        f"baseline_score: {baseline_score:.2f}\n"
        f"best_score: {best['score']:.2f}\n"
        f"improvement: {improved:+.2f}\n"
        f"best_overrides: {best['overrides']}\n"
    )

    dst_path = os.path.join(TUNED_DIR, bot + ".py")
    if best["overrides"]:
        _patch_bot_file(src_path, dst_path, best["overrides"], header_note=note)
    else:
        # No improvement found — still write a copy with the note, unchanged.
        with open(src_path) as f:
            src = f.read()
        banner = (
            '"""AUTO-TUNED by backtest/tune.py\n'
            f"{note}"
            "NOTE: no improvement over baseline — keeping original constants.\n\"\"\"\n"
        )
        with open(dst_path, "w") as f:
            f.write(banner + src)

    # Dump trial log for inspection.
    with open(os.path.join(OUT_DIR, f"{bot}_trials.json"), "w") as f:
        json.dump({
            "bot": bot,
            "baseline_score": baseline_score,
            "baseline_pnl": baseline.get("net_pnl"),
            "best_score": best["score"],
            "best_overrides": best["overrides"],
            "improvement": improved,
            "elapsed_sec": round(elapsed, 2),
            "trials": trials_log,
        }, f, indent=2, default=str)

    return {
        "bot": bot,
        "baseline_score": baseline_score,
        "best_score": best["score"],
        "improvement": improved,
        "overrides": best["overrides"],
        "elapsed_sec": round(elapsed, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bot", nargs="?", help="bot name (e.g. spread_farmer)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--trials", type=int, default=60)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--tick-every", type=int, default=40,
                    help="events per emitted state snapshot (higher = faster)")
    args = ap.parse_args()

    events = list(_replay.merged_events())
    print(f"replay: {len(events)} events, tick_every_n={args.tick_every}")

    bots = _params.all_bots() if args.all else ([args.bot] if args.bot else [])
    if not bots:
        ap.error("pass a bot name or --all")

    rows = []
    for b in bots:
        print(f"\n=== tuning {b} x {args.trials} trials ===")
        r = tune_bot(b, trials=args.trials, seed=args.seed, events=events,
                     tick_every_n_events=args.tick_every)
        print(f"  baseline: {r['baseline_score']:+.0f} | best: {r['best_score']:+.0f} | "
              f"Δ {r['improvement']:+.0f} | {r['elapsed_sec']}s")
        print(f"  overrides: {r['overrides']}")
        rows.append(r)

    print("\n=== summary ===")
    rows.sort(key=lambda r: -r["best_score"])
    for r in rows:
        print(f"  {r['bot']:25s} base={r['baseline_score']:+10.0f} "
              f"best={r['best_score']:+10.0f} Δ={r['improvement']:+10.0f}")


if __name__ == "__main__":
    main()
