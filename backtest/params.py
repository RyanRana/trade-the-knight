"""Per-bot tunable parameter registry.

Each entry says: which module-level constants the tuner may override, and
what set of alternative values to sample from (discrete lists keep the
search space small and readable).

Keep these ranges conservative — sampling a MIN_SPREAD of 10 would wreck the
bot. Values listed here should all be defensible.
"""
from __future__ import annotations
from typing import Dict, List, Any

# Discrete search grid per parameter. The tuner combines random picks across
# these lists ("random search over a discrete grid" — classical Karpathy).
TUNABLES: Dict[str, Dict[str, List[Any]]] = {
    "alpha_maker": {
        "MIN_SPREAD":         [0.03, 0.05, 0.08, 0.12],
        "QUOTE_INSIDE":       [0.01, 0.02, 0.03, 0.05],
        "BASE_SIZE_PCT":      [0.004, 0.008, 0.012, 0.02],
        "MAX_INVENTORY_PCT":  [0.02, 0.03, 0.05, 0.08],
        "SKEW_STRENGTH":      [0.4, 0.7, 1.0, 1.3],
        "SR_WINDOW":          [30, 50, 80, 120],
        "MR_BAND_PCT":        [0.005, 0.01, 0.02, 0.03],
        "MR_SIZE_PCT":        [0.02, 0.04, 0.06, 0.10],
        "MAX_SYMBOLS":        [6, 8, 12, 16],
    },
    "spread_farmer": {
        "MIN_SPREAD_BPS":     [20, 40, 60, 100, 150],
        "MIN_INSIDE":         [0.01, 0.02, 0.03, 0.05],
        "EDGE_PCT":           [0.10, 0.15, 0.25, 0.35, 0.45],
        "BASE_SIZE_PCT":      [0.004, 0.006, 0.008, 0.012, 0.016],
        "MAX_INVENTORY_PCT":  [0.01, 0.02, 0.03, 0.05],
        "SKEW_STRENGTH":      [0.5, 0.8, 1.0, 1.2],
        "CV_MAX":             [0.05, 0.08, 0.12, 0.20],
        "MIN_PRINTS":         [10, 20, 30, 50],
        "MR_ENTRY_Z":         [1.0, 1.5, 2.0, 2.5],
        "MR_SIZE_PCT":        [0.02, 0.04, 0.06, 0.08],
    },
    "carry_vault": {
        "FX_CARRY_THRESHOLD": [0.005, 0.01, 0.02, 0.03, 0.05],
        "FX_POS_PCT":         [0.02, 0.05, 0.08, 0.12],
        "BOND_BID_EDGE":      [0.001, 0.002, 0.004, 0.008],
        "BOND_POS_PCT":       [0.05, 0.10, 0.15, 0.20],
        "SANE_MAX_DEV":       [0.03, 0.05, 0.08, 0.12],
    },
    "cross_section_engine": {
        "TOP_K_FRACTION":         [0.20, 0.33, 0.50],
        "GROSS_EXPOSURE_PCT":     [0.10, 0.15, 0.20, 0.30, 0.40],
        "MAX_POS_PCT":            [0.02, 0.04, 0.06, 0.08],
        "MIN_SCORE":              [0.15, 0.30, 0.50, 0.70],
        "REBALANCE_SEC":          [10.0, 20.0, 30.0, 60.0],
        "PRINTS_WINDOW":          [30, 60, 90, 120],
    },
    "qfc_sniper": {
        "TRIGGER_Z":          [0.5, 1.0, 1.5, 2.0, 2.5],
        "SIZE_PCT":           [0.01, 0.02, 0.03, 0.05],
        "HARD_STOP_PCT":      [0.03, 0.05, 0.08, 0.12],
        "MAX_HOLD_OBS":       [1, 2, 3, 5],
        "MAX_HOLD_SEC":       [30.0, 60.0, 90.0, 180.0],
        "SIGMA_EWMA_ALPHA":   [0.05, 0.10, 0.15, 0.25],
    },
    "tick_sniper": {
        "SIGMA_EWMA_ALPHA":   [0.05, 0.10, 0.15, 0.25],
        "MIN_PRINTS_FOR_SIG": [8, 15, 25, 40],
        "MAX_HOLD_PRINTS":    [2, 3, 5, 8],
        "MAX_HOLD_SEC":       [10.0, 30.0, 60.0, 120.0],
        "HARD_STOP_PCT":      [0.008, 0.015, 0.025, 0.04],
        "MIN_RE_ENTRY_SEC":   [0.5, 1.5, 3.0, 6.0],
    },
    "trend_hunter": {
        "CV_MIN":         [0.01, 0.02, 0.05],
        "CV_MAX":         [0.15, 0.20, 0.30, 0.50],
        "MAX_SPREAD_BPS": [80, 150, 300, 500],
        "ENTRY_Z":        [0.8, 1.2, 1.5, 2.0],
        "EXIT_Z":         [0.1, 0.3, 0.5, 0.8],
        "EMA_SPAN":       [20, 40, 60, 100],
        "MAX_POS_PCT":    [0.02, 0.04, 0.06, 0.10],
        "STOP_ATR":       [1.0, 1.5, 2.0, 3.0],
        "TAKE_ATR":       [1.5, 2.0, 2.5, 4.0],
        "MAX_HOLD_SEC":   [120.0, 300.0, 600.0, 1200.0],
    },
    "event_alpha": {
        "PRED_EDGE_MIN":        [0.03, 0.05, 0.08, 0.12],
        "VRP_EDGE_MIN":         [0.05, 0.10, 0.15],
        "TRI_EDGE_MIN":         [0.0005, 0.001, 0.002, 0.005],
        "BAYES_POSTERIOR_EDGE": [0.05, 0.10, 0.15, 0.25],
        "MAX_HOLD_SEC":         [300.0, 900.0, 1800.0, 3600.0],
    },
}


def all_bots():
    return list(TUNABLES.keys())
