#!/usr/bin/env python3
"""Cost-aware three-way walk-forward audit of dynamic crypto pairs trading."""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import Candle  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics  # noqa: E402

BAR_MS = 15 * 60_000
PERIOD_MS = 30 * DAY_MS


@dataclasses.dataclass(frozen=True)
class PairModel:
    left: str
    right: str
    alpha: float
    beta: float
    mean: float
    stddev: float
    correlation: float
    half_life_bars: float

    @property
    def key(self) -> str:
        return f"{self.left}/{self.right}"


def load_histories(cache_dir: Path) -> dict[str, list[Candle]]:
    histories: dict[str, list[Candle]] = {}
    for path in sorted(cache_dir.glob("*-15m-*.json")):
        symbol = path.name.split("-")[0]
        histories[symbol] = [Candle(**row) for row in json.loads(path.read_text())]
    if len(histories) < 10:
        raise RuntimeError("At least ten cached futures histories are required")
    return histories


def aligned_closes(
    histories: dict[str, list[Candle]],
) -> tuple[list[int], dict[str, np.ndarray], dict[str, dict[int, int]]]:
    common = set(candle.open_time for candle in next(iter(histories.values())))
    by_time: dict[str, dict[int, Candle]] = {}
    for symbol, candles in histories.items():
        rows = {candle.open_time: candle for candle in candles}
        by_time[symbol] = rows
        common.intersection_update(rows)
    times = sorted(common)
    closes = {
        symbol: np.asarray([by_time[symbol][timestamp].close for timestamp in times], dtype=np.float64)
        for symbol in histories
    }
    indexes = {symbol: {timestamp: index for index, timestamp in enumerate(times)} for symbol in histories}
    return times, closes, indexes


def estimate_pair(
    left: str,
    right: str,
    left_prices: np.ndarray,
    right_prices: np.ndarray,
) -> PairModel | None:
    x = np.log(right_prices)
    y = np.log(left_prices)
    variance = float(np.var(x))
    if variance <= 1e-12:
        return None
    beta = float(np.cov(x, y, ddof=0)[0, 1] / variance)
    if not 0.2 <= beta <= 5.0:
        return None
    alpha = float(np.mean(y) - beta * np.mean(x))
    residual = y - alpha - beta * x
    stddev = float(np.std(residual))
    if stddev <= 1e-5:
        return None
    previous = residual[:-1] - float(np.mean(residual[:-1]))
    current = residual[1:] - float(np.mean(residual[1:]))
    denominator = float(np.dot(previous, previous))
    if denominator <= 1e-12:
        return None
    phi = float(np.dot(previous, current) / denominator)
    if not 0.0 < phi < 0.9999:
        return None
    half_life = -math.log(2.0) / math.log(phi)
    left_returns = np.diff(np.log(left_prices[::4]))
    right_returns = np.diff(np.log(right_prices[::4]))
    correlation = float(np.corrcoef(left_returns, right_returns)[0, 1])
    if not math.isfinite(correlation):
        return None
    return PairModel(left, right, alpha, beta, float(np.mean(residual)), stddev, correlation, half_life)


def select_pairs(
    histories: dict[str, list[Candle]],
    times: list[int],
    closes: dict[str, np.ndarray],
    period_start: int,
    maximum_pairs: int = 12,
) -> list[PairModel]:
    left_time = period_start - 60 * DAY_MS
    start_index = int(np.searchsorted(times, left_time, side="left"))
    end_index = int(np.searchsorted(times, period_start, side="left"))
    if end_index - start_index < 30 * 96:
        return []
    candidates: list[tuple[float, PairModel]] = []
    for left, right in itertools.combinations(sorted(histories), 2):
        model = estimate_pair(
            left,
            right,
            closes[left][start_index:end_index],
            closes[right][start_index:end_index],
        )
        if model is None:
            continue
        if model.correlation < 0.72 or not 4 <= model.half_life_bars <= 192:
            continue
        score = model.correlation / math.sqrt(model.half_life_bars)
        candidates.append((score, model))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [model for _score, model in candidates[:maximum_pairs]]


def build_period_models(
    histories: dict[str, list[Candle]],
    times: list[int],
    closes: dict[str, np.ndarray],
    start_ms: int,
    end_ms: int,
) -> dict[int, list[PairModel]]:
    first_period = start_ms - start_ms % PERIOD_MS
    models: dict[int, list[PairModel]] = {}
    period = first_period
    while period < end_ms:
        models[period] = select_pairs(histories, times, closes, period)
        period += PERIOD_MS
    return models


def period_key(timestamp: int) -> int:
    return timestamp - timestamp % PERIOD_MS


def zscore(model: PairModel, left_price: float, right_price: float) -> float:
    spread = math.log(left_price) - model.alpha - model.beta * math.log(right_price)
    return (spread - model.mean) / model.stddev


def summarize(trades: list[dict[str, Any]], start_ms: int, end_ms: int) -> dict[str, Any]:
    winners = [trade for trade in trades if float(trade["net_pnl"]) > 0]
    losers = [trade for trade in trades if float(trade["net_pnl"]) < 0]
    profit = sum(float(trade["net_pnl"]) for trade in winners)
    loss = abs(sum(float(trade["net_pnl"]) for trade in losers))
    balance = 10_000.0
    peak = balance
    drawdown = 0.0
    for trade in sorted(trades, key=lambda item: int(item["exit_time"])):
        balance += float(trade["net_pnl"])
        peak = max(peak, balance)
        drawdown = max(drawdown, (peak - balance) / peak * 100 if peak else 0.0)
    days = max((end_ms - start_ms) / DAY_MS, 1 / 24)
    return {
        "trades": len(trades),
        "trades_per_day": round(len(trades) / days, 2),
        "win_rate": round(len(winners) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(profit / loss, 3) if loss else None,
        "average_realized_r": round(
            sum(float(trade["realized_r"]) for trade in trades) / len(trades), 4
        ) if trades else 0.0,
        "return_percent": round((balance / 10_000.0 - 1) * 100, 2),
        "max_drawdown_percent": round(drawdown, 2),
        "trade_log": trades,
    }


def run_backtest(
    histories: dict[str, list[Candle]],
    times: list[int],
    closes: dict[str, np.ndarray],
    models: dict[int, list[PairModel]],
    start_ms: int,
    end_ms: int,
    entry_z: float,
    exit_z: float,
    stop_z: float,
    max_holding_bars: int,
    fee_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    time_index = {timestamp: index for index, timestamp in enumerate(times)}
    active: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    previous_z: dict[tuple[int, str], float] = {}
    trades: list[dict[str, Any]] = []
    daily_count: dict[str, int] = defaultdict(int)
    balance = 10_000.0
    max_positions = 4
    maximum_daily = 6
    round_trip_cost = 2 * (fee_bps + slippage_bps) / 10_000.0

    for timestamp in (item for item in times if start_ms <= item < end_ms):
        index = time_index[timestamp]
        day = dt.datetime.fromtimestamp(timestamp / 1000, dt.timezone.utc).date().isoformat()
        used_symbols = {
            symbol
            for position in active.values()
            for symbol in (position["model"].left, position["model"].right)
        }
        for order in pending:
            model = order["model"]
            if (
                model.key in active
                or model.left in used_symbols
                or model.right in used_symbols
                or len(active) >= max_positions
                or daily_count[day] >= maximum_daily
            ):
                continue
            left_entry = float(closes[model.left][index])
            right_entry = float(closes[model.right][index])
            entry_score = zscore(model, left_entry, right_entry)
            if abs(entry_score) >= stop_z:
                continue
            side = -1.0 if entry_score > 0 else 1.0
            active[model.key] = {
                "model": model,
                "side": side,
                "entry_time": timestamp,
                "entry_index": index,
                "entry_z": entry_score,
                "left_entry": left_entry,
                "right_entry": right_entry,
                "bars": 0,
                "risk_cash": balance * 0.15 / 100,
            }
            used_symbols.update((model.left, model.right))
            daily_count[day] += 1
        pending = []

        for key, position in list(active.items()):
            if index <= int(position["entry_index"]):
                continue
            model = position["model"]
            position["bars"] = int(position["bars"]) + 1
            left_exit = float(closes[model.left][index])
            right_exit = float(closes[model.right][index])
            current_z = zscore(model, left_exit, right_exit)
            reason = ""
            if abs(current_z) <= exit_z:
                reason = "mean_reversion"
            elif abs(current_z) >= stop_z:
                reason = "spread_stop"
            elif int(position["bars"]) >= max_holding_bars:
                reason = "time_exit"
            if not reason:
                continue
            beta = abs(float(model.beta))
            gross_return = float(position["side"]) * (
                math.log(left_exit / float(position["left_entry"]))
                - model.beta * math.log(right_exit / float(position["right_entry"]))
            ) / (1.0 + beta)
            net_return = gross_return - round_trip_cost
            risk_z = max(stop_z - abs(float(position["entry_z"])), 0.25)
            risk_return = risk_z * model.stddev / (1.0 + beta)
            realized_r = net_return / max(risk_return, 1e-8)
            net_pnl = realized_r * float(position["risk_cash"])
            balance += net_pnl
            trades.append(
                {
                    "pair": model.key,
                    "side": "long_spread" if float(position["side"]) > 0 else "short_spread",
                    "entry_time": int(position["entry_time"]),
                    "exit_time": timestamp,
                    "entry_z": round(float(position["entry_z"]), 4),
                    "exit_z": round(current_z, 4),
                    "realized_r": round(realized_r, 4),
                    "net_pnl": round(net_pnl, 4),
                    "reason": reason,
                    "bars_held": int(position["bars"]),
                }
            )
            active.pop(key)

        current_models = models.get(period_key(timestamp), [])
        if not current_models or len(active) >= max_positions or daily_count[day] >= maximum_daily:
            continue
        used_symbols = {
            symbol
            for position in active.values()
            for symbol in (position["model"].left, position["model"].right)
        }
        candidates: list[tuple[float, PairModel]] = []
        for model in current_models:
            score = zscore(model, float(closes[model.left][index]), float(closes[model.right][index]))
            prior_key = (period_key(timestamp), model.key)
            prior = previous_z.get(prior_key, score)
            previous_z[prior_key] = score
            crossed = abs(prior) < entry_z <= abs(score)
            if (
                crossed
                and abs(score) < stop_z
                and model.key not in active
                and model.left not in used_symbols
                and model.right not in used_symbols
            ):
                candidates.append((abs(score) - entry_z, model))
        free = min(max_positions - len(active), maximum_daily - daily_count[day])
        selected_symbols = set(used_symbols)
        for _strength, model in sorted(candidates, key=lambda item: item[0], reverse=True):
            if free <= 0:
                break
            if model.left in selected_symbols or model.right in selected_symbols:
                continue
            pending.append({"model": model})
            selected_symbols.update((model.left, model.right))
            free -= 1

    return summarize(trades, start_ms, end_ms)


def compact(result: dict[str, Any]) -> dict[str, Any]:
    return {key: result.get(key) for key in (
        "trades", "trades_per_day", "win_rate", "profit_factor",
        "average_realized_r", "return_percent", "max_drawdown_percent",
    )}


def monthly(trades: list[dict[str, Any]], start: int, count: int) -> list[dict[str, Any]]:
    return [
        basic_metrics(trades, start + month * 30 * DAY_MS, start + (month + 1) * 30 * DAY_MS)
        for month in range(count)
    ]


def main() -> int:
    histories = load_histories(ROOT / "data/market_cache_orderflow_15m_430d")
    times, closes, _indexes = aligned_closes(histories)
    data_end = times[-1] + BAR_MS
    development_start = data_end - 370 * DAY_MS
    validation_start = data_end - 180 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS
    models = build_period_models(histories, times, closes, development_start, data_end)
    pair_counts = {str(key): len(value) for key, value in models.items()}

    candidates: list[dict[str, Any]] = []
    for entry_z, exit_z, stop_z, holding in itertools.product(
        (1.25, 1.5, 1.75, 2.0),
        (0.25, 0.5),
        (3.0, 3.5),
        (32, 64),
    ):
        result = run_backtest(
            histories, times, closes, models,
            development_start, validation_start,
            entry_z, exit_z, stop_z, holding,
            fee_bps=5.0, slippage_bps=2.0,
        )
        months = monthly(result["trade_log"], development_start, 6)
        candidates.append(
            {
                "parameters": {
                    "entry_z": entry_z,
                    "exit_z": exit_z,
                    "stop_z": stop_z,
                    "max_holding_bars": holding,
                },
                "development": compact(result),
                "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
            }
        )
    eligible = [
        item for item in candidates
        if 3.5 <= float(item["development"]["trades_per_day"] or 0) <= 6.5
        and float(item["development"]["profit_factor"] or 0) >= 1.1
        and float(item["development"]["win_rate"] or 0) >= 45.0
        and int(item["profitable_months"]) >= 4
    ]
    selected = max(
        eligible or candidates,
        key=lambda item: (
            float(item["development"]["profit_factor"] or 0),
            int(item["profitable_months"]),
        ),
    )
    p = selected["parameters"]
    args = (p["entry_z"], p["exit_z"], p["stop_z"], p["max_holding_bars"])
    validation = run_backtest(
        histories, times, closes, models,
        validation_start, holdout_start, *args,
        fee_bps=5.0, slippage_bps=2.0,
    )
    validation_stress = run_backtest(
        histories, times, closes, models,
        validation_start, holdout_start, *args,
        fee_bps=7.0, slippage_bps=4.0,
    )
    validation_months = monthly(validation["trade_log"], validation_start, 4)
    validation_passed = (
        3.5 <= float(validation["trades_per_day"]) <= 6.5
        and float(validation["profit_factor"] or 0) >= 1.1
        and float(validation["win_rate"] or 0) >= 45.0
        and float(validation_stress["profit_factor"] or 0) >= 1.0
        and sum(float(item["net_pnl"]) > 0 for item in validation_months) >= 3
    )
    report: dict[str, Any] = {
        "method": {
            "description": "60d pair formation, monthly walk-forward selection, 190d development, 120d validation, sealed 60d holdout",
            "pair_trade_costs": "fees and slippage on both legs at entry and exit",
            "symbols": sorted(histories),
            "candidate_count": len(candidates),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "period_pair_counts": pair_counts,
        "selected_on_development": selected,
        "validation_120d": compact(validation),
        "validation_stress": compact(validation_stress),
        "validation_months": validation_months,
        "validation_passed": validation_passed,
        "holdout_opened": validation_passed,
        "development_candidates": candidates,
    }
    if validation_passed:
        holdout = run_backtest(
            histories, times, closes, models,
            holdout_start, data_end, *args,
            fee_bps=5.0, slippage_bps=2.0,
        )
        holdout_stress = run_backtest(
            histories, times, closes, models,
            holdout_start, data_end, *args,
            fee_bps=7.0, slippage_bps=4.0,
        )
        report["final_holdout_once"] = compact(holdout)
        report["final_holdout_stress"] = compact(holdout_stress)
        report["holdout_months"] = monthly(holdout["trade_log"], holdout_start, 2)
    output = ROOT / "research/dynamic_pairs_split.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "selected": selected,
        "validation": compact(validation),
        "validation_stress": compact(validation_stress),
        "validation_passed": validation_passed,
        "final_holdout_once": report.get("final_holdout_once"),
        "output": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
