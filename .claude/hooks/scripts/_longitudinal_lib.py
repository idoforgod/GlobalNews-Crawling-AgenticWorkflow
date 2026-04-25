"""Longitudinal analysis library — historical run comparison.

G7 from 2nd reflection: Master Integration needs day-over-day,
week-over-week, month-over-month comparisons to produce meaningful
temporal insights. Without these, each daily run is a disconnected
snapshot and the "future insight" promise collapses.

Functions:
    query_historical_runs(sot_data, days_back, end_date=None)
    compute_delta(current, previous)
    compute_day_over_day(current_metrics, yesterday_metrics)
    compute_week_over_week(this_week_runs, last_week_runs)
    compute_month_over_month(this_month_runs, last_month_runs)
    detect_temporal_anomalies(series, threshold=2.0)

All functions are pure and deterministic.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

try:
    from _semantic_gate_lib import detect_z_score_outliers
except ImportError:  # pragma: no cover
    detect_z_score_outliers = None  # type: ignore


# ===========================================================================
# compute_delta — basic arithmetic
# ===========================================================================

def compute_delta(current, previous) -> dict:
    """Compute absolute + relative + direction delta between two numeric values.

    Args:
        current: numeric (int|float)
        previous: numeric (int|float) or None

    Returns:
        {
          "current": current,
          "previous": previous,
          "absolute": current - previous,
          "relative_pct": (current-previous)/previous * 100, or None if prev=0
          "direction": "up" | "down" | "flat",
        }
    """
    if current is None:
        current = 0
    if previous is None:
        previous = 0

    absolute = current - previous

    if previous == 0:
        if current == 0:
            relative_pct: float | None = 0.0
        else:
            relative_pct = None  # undefined
    else:
        relative_pct = (absolute / previous) * 100.0

    if absolute > 0:
        direction = "up"
    elif absolute < 0:
        direction = "down"
    else:
        direction = "flat"

    return {
        "current": current,
        "previous": previous,
        "absolute": absolute,
        "relative_pct": relative_pct,
        "direction": direction,
    }


# ===========================================================================
# query_historical_runs
# ===========================================================================

def _parse_date(date_str: str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def query_historical_runs(sot_data: dict, days_back: int,
                           end_date: date | None = None) -> list:
    """Query execution.history for runs within the last `days_back` days.

    Args:
        sot_data: full SOT root dict (must have execution.history)
        days_back: window size in days (inclusive)
        end_date: reference end date (defaults to today)

    Returns:
        List of run dicts within the window, sorted desc by date.
    """
    if not isinstance(sot_data, dict):
        return []
    exec_section = sot_data.get("execution")
    if not isinstance(exec_section, dict):
        return []
    history = exec_section.get("history")
    if not isinstance(history, list):
        return []

    if end_date is None:
        end_date = date.today()
    start_date = end_date - timedelta(days=days_back - 1)  # inclusive window

    filtered: list[dict] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        d_str = entry.get("date")
        d_obj = _parse_date(d_str) if isinstance(d_str, str) else None
        if d_obj is None:
            continue
        if start_date <= d_obj <= end_date:
            filtered.append(entry)

    # Sort by date descending (most recent first)
    filtered.sort(key=lambda e: e.get("date", ""), reverse=True)
    return filtered


# ===========================================================================
# compute_day_over_day
# ===========================================================================

def compute_day_over_day(current_metrics: dict,
                          yesterday_metrics: dict | None) -> dict:
    """Compare today's key metrics to yesterday's.

    Returns {metric_name: {current, previous, absolute, relative_pct,
    direction, is_new}} dict. `is_new=True` when metric exists in current
    but not in previous.
    """
    if not isinstance(current_metrics, dict):
        return {}
    if yesterday_metrics is None:
        yesterday_metrics = {}
    if not isinstance(yesterday_metrics, dict):
        yesterday_metrics = {}

    result: dict = {}
    all_keys = set(current_metrics.keys()) | set(yesterday_metrics.keys())
    for key in all_keys:
        cur = current_metrics.get(key)
        prev = yesterday_metrics.get(key)
        is_new = key in current_metrics and key not in yesterday_metrics
        # Only delta numeric metrics
        if isinstance(cur, (int, float)) or isinstance(prev, (int, float)):
            d = compute_delta(cur, prev)
            if is_new:
                d["previous"] = None
                d["is_new"] = True
            else:
                d["is_new"] = False
            result[key] = d
    return result


# ===========================================================================
# compute_week_over_week
# ===========================================================================

def _aggregate_runs(runs: Iterable[dict]) -> dict:
    """Sum numeric key_metrics across a list of runs."""
    totals: dict = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        # A run entry may be {"key_metrics": {...}} or {"articles": ..., ...}
        metrics = run.get("key_metrics", run)
        if not isinstance(metrics, dict):
            continue
        for k, v in metrics.items():
            if isinstance(v, (int, float)):
                totals[k] = totals.get(k, 0) + v
    return totals


def compute_week_over_week(this_week_runs: list,
                            last_week_runs: list) -> dict:
    """Aggregate and compare a week's runs to the previous week's.

    Returns {metric_name: {current_sum, previous_sum, absolute,
    relative_pct, direction}}.
    """
    current_totals = _aggregate_runs(this_week_runs)
    previous_totals = _aggregate_runs(last_week_runs)

    result: dict = {}
    for key in set(current_totals.keys()) | set(previous_totals.keys()):
        cur = current_totals.get(key, 0)
        prev = previous_totals.get(key, 0)
        d = compute_delta(cur, prev)
        result[key] = {
            "current_sum": cur,
            "previous_sum": prev,
            "absolute": d["absolute"],
            "relative_pct": d["relative_pct"],
            "direction": d["direction"],
        }
    return result


def compute_month_over_month(this_month_runs: list,
                              last_month_runs: list) -> dict:
    """Aggregate and compare a month's runs to the previous month's.

    Same structure as compute_week_over_week.
    """
    return compute_week_over_week(this_month_runs, last_month_runs)


# ===========================================================================
# detect_temporal_anomalies
# ===========================================================================

def detect_temporal_anomalies(series: Iterable[float],
                                threshold: float = 2.0) -> list:
    """Return indices of temporal outliers in a time series.

    Delegates to _semantic_gate_lib.detect_z_score_outliers when available,
    otherwise falls back to inline computation.
    """
    if detect_z_score_outliers is not None:
        return detect_z_score_outliers(series, threshold=threshold)
    # Fallback (shouldn't normally hit)
    vals = [float(v) for v in series if v is not None]
    n = len(vals)
    if n < 2:
        return []
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    if var == 0.0:
        return []
    import math
    std = math.sqrt(var)
    return [i for i, v in enumerate(vals) if abs((v - mean) / std) > threshold]
