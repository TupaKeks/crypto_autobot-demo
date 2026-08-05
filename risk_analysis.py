"""Deterministic rolling-window and block-bootstrap risk diagnostics."""

from __future__ import annotations

import math
import random
from typing import Any


DAY_MS = 86_400_000


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * min(1.0, max(0.0, probability))
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def daily_fractional_returns(
    trades: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    risk_percent_by_side: dict[str, float],
) -> list[float]:
    days = max(0, math.ceil((end_ms - start_ms) / DAY_MS))
    returns = [0.0] * days
    for trade in trades:
        entry_time = int(trade.get("entry_time", -1))
        if not (start_ms <= entry_time < end_ms):
            continue
        day_index = min(days - 1, (entry_time - start_ms) // DAY_MS) if days else -1
        if day_index < 0:
            continue
        side = str(trade.get("side", ""))
        risk_percent = float(risk_percent_by_side.get(side, 0.0))
        returns[day_index] += float(trade.get("realized_r", 0.0)) * risk_percent / 100.0
    return returns


def path_metrics(daily_returns: list[float]) -> dict[str, float]:
    equity = 1.0
    peak = equity
    max_drawdown = 0.0
    for daily_return in daily_returns:
        equity *= max(0.0, 1.0 + float(daily_return))
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return {
        "return_percent": (equity - 1.0) * 100.0,
        "max_drawdown_percent": max_drawdown * 100.0,
    }


def _summarize_paths(paths: list[dict[str, float]]) -> dict[str, Any]:
    returns = [item["return_percent"] for item in paths]
    drawdowns = [item["max_drawdown_percent"] for item in paths]
    return {
        "paths": len(paths),
        "probability_profitable_percent": round(
            sum(value > 0 for value in returns) / len(returns) * 100.0,
            2,
        ) if returns else 0.0,
        "probability_drawdown_at_least_5_percent": round(
            sum(value >= 5.0 for value in drawdowns) / len(drawdowns) * 100.0,
            2,
        ) if drawdowns else 0.0,
        "probability_drawdown_at_least_10_percent": round(
            sum(value >= 10.0 for value in drawdowns) / len(drawdowns) * 100.0,
            2,
        ) if drawdowns else 0.0,
        "return_percent": {
            "worst": round(min(returns), 3) if returns else None,
            "p05": round(_percentile(returns, 0.05), 3) if returns else None,
            "median": round(_percentile(returns, 0.50), 3) if returns else None,
            "p95": round(_percentile(returns, 0.95), 3) if returns else None,
            "best": round(max(returns), 3) if returns else None,
        },
        "max_drawdown_percent": {
            "median": round(_percentile(drawdowns, 0.50), 3) if drawdowns else None,
            "p95": round(_percentile(drawdowns, 0.95), 3) if drawdowns else None,
            "worst": round(max(drawdowns), 3) if drawdowns else None,
        },
    }


def rolling_window_risk(
    daily_returns: list[float],
    horizon_days: int = 30,
) -> dict[str, Any]:
    horizon = max(1, int(horizon_days))
    paths = [
        path_metrics(daily_returns[index:index + horizon])
        for index in range(max(0, len(daily_returns) - horizon + 1))
    ]
    return {
        "method": "all overlapping historical windows",
        "horizon_days": horizon,
        **_summarize_paths(paths),
    }


def block_bootstrap_risk(
    daily_returns: list[float],
    *,
    horizon_days: int = 30,
    block_days: int = 5,
    simulations: int = 10_000,
    seed: int = 20_260_805,
) -> dict[str, Any]:
    if not daily_returns:
        return {
            "method": "moving-block bootstrap",
            "horizon_days": int(horizon_days),
            "block_days": int(block_days),
            "simulations": 0,
            **_summarize_paths([]),
        }
    horizon = max(1, int(horizon_days))
    block = min(len(daily_returns), max(1, int(block_days)))
    count = max(1, int(simulations))
    rng = random.Random(seed)
    latest_start = len(daily_returns) - block
    paths: list[dict[str, float]] = []
    for _ in range(count):
        sampled: list[float] = []
        while len(sampled) < horizon:
            start = rng.randint(0, latest_start) if latest_start > 0 else 0
            sampled.extend(daily_returns[start:start + block])
        paths.append(path_metrics(sampled[:horizon]))
    return {
        "method": "moving-block bootstrap",
        "horizon_days": horizon,
        "block_days": block,
        "simulations": count,
        "seed": seed,
        **_summarize_paths(paths),
    }


def max_consecutive_losses(trades: list[dict[str, Any]]) -> int:
    longest = 0
    current = 0
    for trade in sorted(trades, key=lambda item: int(item.get("exit_time", 0))):
        if float(trade.get("net_pnl", 0.0)) < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest
