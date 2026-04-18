"""Replay data/*.jsonl captures as a sorted event stream.

Each emitted event is one of:
  {"kind": "book",       "t": float, "books": {sym: {bids: {px: qty}, asks: {px: qty}}}}
  {"kind": "trade",      "t": float, "trade": {symbol, price, quantity, tick, executed_at}}
  {"kind": "timeseries", "t": float, "series": {name: [{"t","v"}, ...]}}
  {"kind": "assets",     "t": float, "assets": [asset_meta, ...]}

Book snapshots in data/book.jsonl store per-level dicts of order-lists; we flatten
to per-level *total quantity* (a float) because the sim only needs depth.

Trades in data/trades.jsonl come as a rolling window (≤50 most recent) per snapshot.
We dedupe across snapshots by (tick, symbol, price, quantity, executed_at).
"""
from __future__ import annotations
import json, os
from typing import Iterable, Dict, Any, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def _loads(path: str) -> List[dict]:
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


def _flatten_book(raw_book: dict) -> dict:
    """Convert {px_str: [{quantity:...}, ...]} -> {px_str: total_qty_float}."""
    out = {"bids": {}, "asks": {}}
    for side_key in ("bids", "asks"):
        levels = raw_book.get(side_key) or {}
        flat = {}
        for px, val in levels.items():
            try:
                px_f = float(px)
            except (TypeError, ValueError):
                continue
            if px_f <= 0:
                continue
            if isinstance(val, list):
                total = 0.0
                for o in val:
                    try:
                        total += float(o.get("quantity") or o.get("qty") or 0)
                    except (TypeError, ValueError):
                        pass
            else:
                try:
                    total = float(val)
                except (TypeError, ValueError):
                    total = 0.0
            if total > 0:
                flat[px] = total
        out[side_key] = flat
    return out


def _flatten_books_payload(pl: dict) -> dict:
    books = {}
    for sym, rb in (pl or {}).items():
        if not isinstance(rb, dict):
            continue
        if "bids" not in rb and "asks" not in rb:
            continue
        books[sym] = _flatten_book(rb)
    return books


def load_trades_dedup() -> List[dict]:
    """Return unique trades in chronological order."""
    seen = set()
    rows = []
    for line in _loads(os.path.join(DATA, "trades.jsonl")):
        wall_t = line.get("t", 0.0)
        for tr in line.get("payload", []) or []:
            key = (
                tr.get("tick"),
                tr.get("symbol"),
                tr.get("price"),
                tr.get("quantity"),
                tr.get("executed_at"),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "t_wall": wall_t,
                "tick": tr.get("tick") or 0,
                "symbol": tr.get("symbol"),
                "price": float(tr.get("price") or 0),
                "quantity": float(tr.get("quantity") or 0),
                "executed_at": tr.get("executed_at"),
            })
    rows.sort(key=lambda r: (r["tick"], r["symbol"]))
    return rows


def load_books() -> List[dict]:
    """Return chronological book snapshots [{'t': float, 'books': {...}}]."""
    out = []
    for line in _loads(os.path.join(DATA, "book.jsonl")):
        wall_t = line.get("t", 0.0)
        books = _flatten_books_payload(line.get("payload") or {})
        if books:
            out.append({"t": wall_t, "books": books})
    out.sort(key=lambda r: r["t"])
    return out


def load_timeseries_snapshots() -> List[dict]:
    """Return list of {'t': wall_t, 'series': {name: {...latest info...}}}."""
    out = []
    for line in _loads(os.path.join(DATA, "timeseries.jsonl")):
        wall_t = line.get("t", 0.0)
        series = {}
        for s in line.get("payload", []) or []:
            name = s.get("name")
            if not name:
                continue
            series[name] = {
                "name": name,
                "latest_value": s.get("latest_value"),
                "latest_time": s.get("latest_time"),
                "data_points": s.get("data_points"),
                "interval": s.get("interval"),
            }
        if series:
            out.append({"t": wall_t, "series": series})
    out.sort(key=lambda r: r["t"])
    return out


def build_asset_list() -> List[dict]:
    """Synthesize an asset table from the symbols present in book.jsonl.

    We don't have a dumped asset metadata file, so we infer: ticker-style
    symbols (HILL/QFC/LIVI/etc.) are treated as 'spot'; xxxRUD pairs as 'forex';
    T##LEAD-style as 'prediction'; everything else defaults to 'spot'.
    """
    syms = set()
    for snap in load_books():
        syms.update(snap["books"].keys())
    # Also pull from trades + tape to be comprehensive.
    try:
        for line in _loads(os.path.join(DATA, "trades.jsonl")):
            for tr in line.get("payload", []) or []:
                if tr.get("symbol"):
                    syms.add(tr["symbol"])
    except Exception:
        pass

    assets = []
    for s in sorted(syms):
        if s == "RUD":
            continue
        t = _infer_type(s)
        assets.append({
            "symbol": s,
            "asset_type": t,
            "tradable": True,
            "halted": False,
        })
    return assets


def _infer_type(sym: str) -> str:
    u = sym.upper()
    if u.endswith("RUD"):
        return "forex"
    if u.startswith("T") and "LEAD" in u:
        return "prediction"
    if u in {"QFC", "HILL", "LIVI", "SCA", "RITE", "SCAR", "YARD", "SCIX", "RUX", "RUVX", "PASS"}:
        return "spot"
    if u.startswith("S") and len(u) <= 8:
        # SRED/SBLUE/SGREEN/SYELLOW/SCIX — loose guess: predictions
        return "prediction" if u not in {"SCA", "SCAR"} else "spot"
    return "spot"


def merged_events() -> Iterable[dict]:
    """Yield events sorted by wall-clock timestamp.

    Book snapshots carry a 't' per JSONL line (wall time). Trades carry their
    own executed_at (string) or tick; we align them to the nearest book
    snapshot interval by spreading them linearly across the book timeline.
    """
    books = load_books()
    trades = load_trades_dedup()
    ts_snaps = load_timeseries_snapshots()

    # Align trades to wall-clock using their tick number (monotonic int).
    if trades and books:
        min_tick = min(t["tick"] for t in trades) or 1
        max_tick = max(t["tick"] for t in trades) or (min_tick + 1)
        min_t = books[0]["t"]
        max_t = books[-1]["t"]
        span = max(max_t - min_t, 1e-6)
        span_ticks = max(max_tick - min_tick, 1)
        for tr in trades:
            tr["t"] = min_t + (tr["tick"] - min_tick) / span_ticks * span
    else:
        for i, tr in enumerate(trades):
            tr["t"] = float(i)

    events = []
    for b in books:
        events.append({"kind": "book", "t": b["t"], "books": b["books"]})
    for tr in trades:
        events.append({"kind": "trade", "t": tr["t"], "trade": tr})
    for ts in ts_snaps:
        events.append({"kind": "timeseries", "t": ts["t"], "series": ts["series"]})

    events.sort(key=lambda e: e["t"])
    return events


def summarize() -> dict:
    books = load_books()
    trades = load_trades_dedup()
    ts_snaps = load_timeseries_snapshots()
    return {
        "book_snaps": len(books),
        "trades": len(trades),
        "trade_symbols": sorted({t["symbol"] for t in trades}),
        "ts_snaps": len(ts_snaps),
        "span_sec": (books[-1]["t"] - books[0]["t"]) if len(books) >= 2 else 0.0,
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(summarize())
