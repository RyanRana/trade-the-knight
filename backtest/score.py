"""Backtest runner + scoring.

run_bot(bot_path, overrides) loads a single bot file into an isolated module,
patches its module constants with `overrides`, wires up the sim + mock client,
drives the event stream, and returns a stats dict.

Score is a scalar the tuner maximizes:
    score = final_equity - starting_cap         (net PnL in RUD)
    plus small penalty for extreme max-inventory to punish degenerate strategies
    that just eat risk.
"""
from __future__ import annotations
import os, sys, importlib.util, inspect, types, traceback, logging
from typing import Optional, Dict, Any, Callable, Iterable

from . import replay as _replay
from . import sim as _sim

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISHED_DIR = os.path.join(ROOT, "published_bots")
TUNED_DIR = os.path.join(ROOT, "published_bots_tuned")

# Quiet the bots' chatter during backtests; run_all can reset this.
logging.basicConfig(level=logging.ERROR)
logging.getLogger().setLevel(logging.ERROR)
for _n in ("alpha_maker", "carry_vault", "xsec", "event_alpha", "qfc_sniper",
           "tick_sniper", "spread_farmer", "trend_hunter"):
    logging.getLogger(_n).setLevel(logging.CRITICAL + 1)


# ---------------------------------------------------------------------------
# Event stream builder — converts (event_list, sim) into per-tick snapshots.
# ---------------------------------------------------------------------------

def build_feeder(events: list, sim: _sim.SimState,
                 tick_every_n_events: int = 25) -> Iterable[dict]:
    """Apply events to sim; yield a state snapshot every N events and at EOF.

    Running one yield per event is overkill (150k+ ticks) and slow. Batching
    ~25 events per yield still gives bots many opportunities to act while
    keeping total iterations in the low thousands.
    """
    count = 0
    last_book_t = None
    for ev in events:
        t = ev["t"]
        _sim._SIM_CLOCK.set(t)
        kind = ev["kind"]
        if kind == "book":
            sim.apply_book_update(ev["books"])
            last_book_t = t
        elif kind == "trade":
            sim.apply_trade_print(ev["trade"])
        elif kind == "timeseries":
            sim.apply_timeseries(ev["series"])
        count += 1
        if count % tick_every_n_events == 0 and sim.books:
            yield sim.snapshot_state()
    # Final snapshot so bots see the closing state.
    if sim.books:
        yield sim.snapshot_state()


# ---------------------------------------------------------------------------
# Bot loader — imports bot file with mock knight_trader + time patches.
# ---------------------------------------------------------------------------

def _load_bot_module(path: str, module_name: str, overrides: Optional[Dict[str, Any]] = None):
    """Load a bot .py as a module. Inject overrides on module globals right
    after exec, before we instantiate the bot class.

    We do this in two steps so the constants end up visible to closures in
    the bot's methods too (they read the module-level name at call time).
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    if overrides:
        for k, v in overrides.items():
            if hasattr(mod, k):
                setattr(mod, k, v)
    return mod


def _find_runnable(mod) -> Optional[Callable]:
    """Prefer a class with a no-arg run() method; fall back to a module-level run()."""
    for name, obj in inspect.getmembers(mod, inspect.isclass):
        if obj.__module__ != mod.__name__:
            continue
        if hasattr(obj, "run") and callable(getattr(obj, "run")):
            try:
                inst = obj()
            except Exception:
                continue
            return inst.run
    if hasattr(mod, "run") and callable(mod.run):
        return mod.run
    return None


# ---------------------------------------------------------------------------
# One backtest invocation.
# ---------------------------------------------------------------------------

def run_bot(bot_path: str,
            overrides: Optional[Dict[str, Any]] = None,
            starting_cap: float = 1_000_000.0,
            events: Optional[list] = None,
            tick_every_n_events: int = 25) -> Dict[str, Any]:
    """Run one bot through the full event stream. Returns a stats dict."""
    base = os.path.splitext(os.path.basename(bot_path))[0]
    mod_name = f"_bt_{base}_{id(overrides)}"

    if events is None:
        events = list(_replay.merged_events())

    sim = _sim.SimState(starting_cap=starting_cap)
    sim.assets = _replay.build_asset_list()
    # Preload timeseries with a seed so bots reading ior_rate get a value.
    sim.timeseries.setdefault("ior_rate", [{"t": 0.0, "v": 0.035}])

    client = _sim.MockExchangeClient(sim, bot_id="SIMBOT")
    _sim.install_knight_trader_module(client)
    _sim._SIM_CLOCK.set(0.0)
    _sim.install_time_patch()

    os.environ["BOT_ID"] = "SIMBOT"
    os.environ.setdefault("EXCHANGE_URL", "http://sim.local")

    stats: Dict[str, Any] = {
        "bot": base,
        "overrides": dict(overrides or {}),
        "ok": False,
        "error": None,
    }

    try:
        mod = _load_bot_module(bot_path, mod_name, overrides)
        # Re-sync BOT_ID if bot captured it at import time.
        if hasattr(mod, "BOT_ID"):
            mod.BOT_ID = "SIMBOT"
        runnable = _find_runnable(mod)
        if runnable is None:
            stats["error"] = "no runnable class/run() found"
            return stats

        feeder = build_feeder(events, sim, tick_every_n_events=tick_every_n_events)
        client._feeder = feeder
        try:
            runnable()
        except StopIteration:
            pass
        stats["ok"] = True
    except Exception as exc:
        stats["error"] = f"{type(exc).__name__}: {exc}"
        stats["traceback"] = traceback.format_exc()
    finally:
        _sim.restore_time()
        sys.modules.pop("knight_trader", None)
        sys.modules.pop(mod_name, None)

    # Close out: flatten at last known mid for valuation consistency.
    closing_equity = sim.equity()
    gross_inv = sum(abs(q) * (sim.mid(s) or sim.last_trade_px.get(s, 0.0) or 0.0)
                    for s, q in sim.positions.items())

    stats.update({
        "starting_cap": sim.starting_cap,
        "final_cash": sim.cash,
        "final_equity": closing_equity,
        "realized_pnl": sim.realized_pnl,
        "net_pnl": closing_equity - sim.starting_cap,
        "fills": len(sim.fills),
        "open_orders_left": len(sim.open_orders),
        "max_gross_inventory": gross_inv,
        "symbols_touched": sorted({f["sym"] for f in sim.fills}),
    })
    stats["score"] = _score(stats)
    return stats


def _score(stats: Dict[str, Any]) -> float:
    """Scalar objective the tuner maximizes.

    Primary: net PnL. Penalty for large residual inventory at session end
    (stuff the bot never closed out).
    """
    net = float(stats.get("net_pnl", 0.0) or 0.0)
    stuck = float(stats.get("max_gross_inventory", 0.0) or 0.0)
    starting = float(stats.get("starting_cap", 1_000_000.0) or 1.0)
    # 5% of stuck inventory counts as drag vs PnL (simulating true close-out cost).
    drag = 0.05 * stuck
    # Heavy penalty if the bot errored out (so bad overrides score poorly).
    if not stats.get("ok"):
        return -starting
    return net - drag
