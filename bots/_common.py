"""
_common.py — shared helpers. This file is NOT uploaded (each bot is
self-contained); it's a reference copy of the defensive SDK accessors used
across the fleet. Keeping this in the repo makes it easier to review the
shared patterns in one place and to refactor them if the real SDK schema
differs from our assumptions on competition day.
"""

from typing import Optional, Tuple


def num(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def asset_type(asset: dict) -> str:
    return (asset.get("asset_type") or asset.get("type") or "").lower()


def symbol_of(asset: dict) -> Optional[str]:
    return asset.get("symbol") or asset.get("id") or asset.get("name")


def is_tradable(asset: dict) -> bool:
    if asset.get("halted"):
        return False
    if "tradable" in asset and not asset["tradable"]:
        return False
    return True


def best_prices(book: dict) -> Tuple[Optional[float], Optional[float]]:
    bids = book.get("bids") or {}
    asks = book.get("asks") or {}
    bp = [num(k) for k in bids.keys() if num(k) > 0]
    ap = [num(k) for k in asks.keys() if num(k) > 0]
    return (max(bp) if bp else None, min(ap) if ap else None)


def team_equity(team_state: dict) -> float:
    for key in ("total_equity", "equity", "leaderboard_equity", "net_equity"):
        if key in team_state:
            return num(team_state[key])
    rud = num(team_state.get("rud") or team_state.get("treasury", {}).get("rud"))
    cap = 0.0
    for b in (team_state.get("bots") or {}).values():
        cap += num(b.get("capital") or b.get("allocated_capital"))
    return rud + cap


def bot_record(team_state: dict, bot_id: str) -> dict:
    bots = team_state.get("bots") or team_state.get("portfolio", {}).get("bots") or {}
    if isinstance(bots, dict):
        return bots.get(bot_id) or {}
    return next((b for b in bots if b.get("id") == bot_id or b.get("bot_id") == bot_id), {})


def bot_capital(team_state: dict, bot_id: str) -> float:
    b = bot_record(team_state, bot_id)
    return num(b.get("capital") or b.get("allocated_capital") or b.get("uncommitted_capital"))


def bot_positions(team_state: dict, bot_id: str) -> dict:
    b = bot_record(team_state, bot_id)
    raw = b.get("positions") or b.get("inventory") or {}
    out = {}
    if isinstance(raw, dict):
        for sym, v in raw.items():
            if isinstance(v, dict):
                out[sym] = num(v.get("quantity") or v.get("qty") or v.get("size"))
            else:
                out[sym] = num(v)
    elif isinstance(raw, list):
        for row in raw:
            sym = row.get("symbol") or row.get("asset")
            if sym:
                out[sym] = num(row.get("quantity") or row.get("qty") or row.get("size"))
    return out


def bot_position(team_state: dict, bot_id: str, symbol: str) -> float:
    return bot_positions(team_state, bot_id).get(symbol, 0.0)


def latest_ior(client) -> float:
    for name in ("ior_rate", "fed_rate"):
        try:
            pts = client.get_timeseries(name, limit=1) or []
            if pts:
                last = pts[-1]
                v = last.get("v") if isinstance(last, dict) else last
                r = num(v)
                if r > 0:
                    return r
        except Exception:
            continue
    return 0.05
