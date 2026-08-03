#!/usr/bin/env python3
"""Walk-forward audit of same-day hourly return predictability.

The experiment is deliberately separate from the live bot.  A relationship is
eligible only when it was fitted and checked on data available before the day
being traded.  The final 60-day holdout is evaluated only when explicitly
requested with --holdout-config.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

try:
    from ..bot import Candle
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bot import Candle


DAY_MS = 86_400_000
HOUR_MS = 3_600_000


@dataclasses.dataclass(frozen=True)
class HourBar:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    close_time: int
    candles: tuple[Candle, ...]
    atr: float | None = None

    @property
    def return_value(self) -> float:
        return self.close / self.open - 1.0


@dataclasses.dataclass(frozen=True)
class Relationship:
    predictor_hour: int
    intercept: float
    slope: float
    calibration_signals: int
    calibration_win_rate: float
    calibration_edge: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.demo.regime-scalp.example.json")
    parser.add_argument("--cache-dir", default="data/market_cache_15m_430d")
    parser.add_argument("--output", default="research/hourly_return_predictability.json")
    parser.add_argument(
        "--holdout-config",
        help="JSON object such as '{\"min_prediction\":0.0004,\"min_eval_win_rate\":0.53}'",
    )
    return parser.parse_args()


def load_histories(cache_dir: Path, symbols: Iterable[str]) -> dict[str, list[Candle]]:
    histories: dict[str, list[Candle]] = {}
    for symbol in symbols:
        matches = sorted(cache_dir.glob(f"{symbol}-15m-*.json"))
        if not matches:
            raise FileNotFoundError(f"No cached 15m history for {symbol}")
        rows = json.loads(matches[-1].read_text(encoding="utf-8"))
        histories[symbol] = [Candle(**row) for row in rows]
    return histories


def aggregate_hours(candles: list[Candle], atr_length: int = 14) -> dict[tuple[str, int], HourBar]:
    groups: dict[int, list[Candle]] = defaultdict(list)
    for candle in candles:
        hour_start = candle.open_time - candle.open_time % HOUR_MS
        groups[hour_start].append(candle)

    raw: list[HourBar] = []
    for hour_start in sorted(groups):
        items = sorted(groups[hour_start], key=lambda item: item.open_time)
        expected = [hour_start + offset * 900_000 for offset in range(4)]
        if len(items) != 4 or [item.open_time for item in items] != expected:
            continue
        raw.append(
            HourBar(
                open_time=hour_start,
                open=items[0].open,
                high=max(item.high for item in items),
                low=min(item.low for item in items),
                close=items[-1].close,
                close_time=items[-1].close_time,
                candles=tuple(items),
            )
        )

    result: dict[tuple[str, int], HourBar] = {}
    true_ranges: list[float] = []
    previous_close: float | None = None
    for index, bar in enumerate(raw):
        true_range = bar.high - bar.low
        if previous_close is not None:
            true_range = max(true_range, abs(bar.high - previous_close), abs(bar.low - previous_close))
        true_ranges.append(true_range)
        previous_close = bar.close
        atr_value = None
        if index >= atr_length - 1:
            atr_value = sum(true_ranges[index - atr_length + 1 : index + 1]) / atr_length
        timestamp = dt.datetime.fromtimestamp(bar.open_time / 1000, dt.timezone.utc)
        result[(timestamp.date().isoformat(), timestamp.hour)] = dataclasses.replace(bar, atr=atr_value)
    return result


def fit_ols(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    if len(pairs) < 12:
        return None
    mean_x = sum(x for x, _ in pairs) / len(pairs)
    mean_y = sum(y for _, y in pairs) / len(pairs)
    denominator = sum((x - mean_x) ** 2 for x, _ in pairs)
    if denominator <= 1e-18:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / denominator
    return mean_y - slope * mean_x, slope


def date_range(start_ms: int, end_ms: int) -> list[str]:
    start = dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc).date()
    end = dt.datetime.fromtimestamp((end_ms - 1) / 1000, dt.timezone.utc).date()
    return [(start + dt.timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def training_pairs(
    hours: dict[tuple[str, int], HourBar],
    days: Iterable[str],
    predictor_hour: int,
    target_hour: int,
) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for day in days:
        predictor = hours.get((day, predictor_hour))
        target = hours.get((day, target_hour))
        if predictor is not None and target is not None:
            pairs.append((predictor.return_value, target.return_value))
    return pairs


def select_relationships(
    hourly: dict[str, dict[tuple[str, int], HourBar]],
    symbols: list[str],
    rebalance_ms: int,
    min_prediction: float,
    min_eval_win_rate: float,
) -> dict[tuple[str, int], Relationship]:
    older_days = date_range(rebalance_ms - 90 * DAY_MS, rebalance_ms - 45 * DAY_MS)
    newer_days = date_range(rebalance_ms - 45 * DAY_MS, rebalance_ms)
    full_days = date_range(rebalance_ms - 90 * DAY_MS, rebalance_ms)
    chosen: dict[tuple[str, int], Relationship] = {}

    for symbol in symbols:
        bars = hourly[symbol]
        for target_hour in range(1, 24):
            best: tuple[float, int, float, float, int, float, float] | None = None
            for predictor_hour in range(target_hour):
                fit = fit_ols(training_pairs(bars, older_days, predictor_hour, target_hour))
                if fit is None:
                    continue
                intercept, slope = fit
                evaluations: list[float] = []
                wins = 0
                for x_value, y_value in training_pairs(bars, newer_days, predictor_hour, target_hour):
                    prediction = intercept + slope * x_value
                    if abs(prediction) < min_prediction:
                        continue
                    signed_return = y_value if prediction > 0 else -y_value
                    evaluations.append(signed_return)
                    wins += int(signed_return > 0)
                if len(evaluations) < 8:
                    continue
                win_rate = wins / len(evaluations)
                edge = sum(evaluations) / len(evaluations)
                if win_rate < min_eval_win_rate or edge <= 0:
                    continue
                score = edge * math.sqrt(len(evaluations))
                candidate = (score, predictor_hour, intercept, slope, len(evaluations), win_rate, edge)
                if best is None or candidate[0] > best[0]:
                    best = candidate
            if best is None:
                continue
            _, predictor_hour, _, _, count, win_rate, edge = best
            refit = fit_ols(training_pairs(bars, full_days, predictor_hour, target_hour))
            if refit is None:
                continue
            chosen[(symbol, target_hour)] = Relationship(
                predictor_hour=predictor_hour,
                intercept=refit[0],
                slope=refit[1],
                calibration_signals=count,
                calibration_win_rate=win_rate,
                calibration_edge=edge,
            )
    return chosen


def slipped(price: float, side: str, slippage_bps: float) -> float:
    multiplier = 1 + slippage_bps / 10_000 if side == "buy" else 1 - slippage_bps / 10_000
    return price * multiplier


def simulate_trade(
    symbol: str,
    side: str,
    prediction: float,
    target_bar: HourBar,
    previous_bar: HourBar,
    balance: float,
    risk_percent: float = 0.15,
    stop_atr: float = 1.0,
    target_atr: float = 2.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    leverage: float = 2.0,
) -> dict[str, Any] | None:
    if previous_bar.atr is None or previous_bar.atr <= 0:
        return None
    entry_side = "buy" if side == "long" else "sell"
    exit_side = "sell" if side == "long" else "buy"
    entry = slipped(target_bar.open, entry_side, slippage_bps)
    stop_distance = previous_bar.atr * stop_atr
    target_distance = previous_bar.atr * target_atr
    risk_cash = balance * risk_percent / 100
    quantity = min(risk_cash / stop_distance, balance * leverage * 0.95 / entry)
    if quantity <= 0 or not math.isfinite(quantity):
        return None
    stop = entry - stop_distance if side == "long" else entry + stop_distance
    target = entry + target_distance if side == "long" else entry - target_distance
    raw_exit = target_bar.close
    reason = "time"
    exit_time = target_bar.close_time
    for candle in target_bar.candles:
        stop_hit = candle.low <= stop if side == "long" else candle.high >= stop
        target_hit = candle.high >= target if side == "long" else candle.low <= target
        if stop_hit:
            raw_exit = stop
            reason = "stop"
            exit_time = candle.close_time
            break
        if target_hit:
            raw_exit = target
            reason = "target"
            exit_time = candle.close_time
            break
    exit_price = slipped(raw_exit, exit_side, slippage_bps)
    gross = (exit_price - entry) * quantity if side == "long" else (entry - exit_price) * quantity
    fees = (entry + exit_price) * quantity * fee_bps / 10_000
    net = gross - fees
    return {
        "symbol": symbol,
        "side": side,
        "entry_time": target_bar.open_time,
        "exit_time": exit_time,
        "entry": round(entry, 8),
        "exit": round(exit_price, 8),
        "stop": round(stop, 8),
        "target": round(target, 8),
        "prediction": round(prediction, 8),
        "net_pnl": round(net, 4),
        "realized_r": round(net / risk_cash, 4),
        "reason": reason,
    }


def summarize(trades: list[dict[str, Any]], initial_balance: float, final_balance: float, days: float) -> dict[str, Any]:
    winners = [trade for trade in trades if trade["net_pnl"] > 0]
    losers = [trade for trade in trades if trade["net_pnl"] < 0]
    gross_profit = sum(trade["net_pnl"] for trade in winners)
    gross_loss = abs(sum(trade["net_pnl"] for trade in losers))
    equity = initial_balance
    peak = initial_balance
    max_drawdown = 0.0
    for trade in sorted(trades, key=lambda item: (item["exit_time"], item["symbol"])):
        equity += trade["net_pnl"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
    return {
        "trades": len(trades),
        "trades_per_day": round(len(trades) / days, 2),
        "win_rate": round(len(winners) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "average_realized_r": round(sum(trade["realized_r"] for trade in trades) / len(trades), 4) if trades else 0.0,
        "return_percent": round((final_balance / initial_balance - 1) * 100, 2),
        "max_drawdown_percent": round(max_drawdown, 2),
    }


def run_walk_forward(
    hourly: dict[str, dict[tuple[str, int], HourBar]],
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    min_prediction: float,
    min_eval_win_rate: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    initial_balance = 10_000.0
    balance = initial_balance
    trades: list[dict[str, Any]] = []
    models_by_period: dict[int, dict[tuple[str, int], Relationship]] = {}
    day_trade_counts: dict[str, int] = defaultdict(int)

    for day in date_range(start_ms, end_ms):
        day_start = int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        period = max(0, (day_start - start_ms) // (30 * DAY_MS))
        rebalance_ms = start_ms + period * 30 * DAY_MS
        if period not in models_by_period:
            models_by_period[period] = select_relationships(
                hourly, symbols, rebalance_ms, min_prediction, min_eval_win_rate
            )
        relationships = models_by_period[period]
        for target_hour in range(1, 24):
            if day_trade_counts[day] >= 6:
                break
            candidates: list[tuple[float, str, str, float, HourBar, HourBar]] = []
            for symbol in symbols:
                relationship = relationships.get((symbol, target_hour))
                target_bar = hourly[symbol].get((day, target_hour))
                previous_bar = hourly[symbol].get((day, target_hour - 1))
                predictor = hourly[symbol].get((day, relationship.predictor_hour)) if relationship else None
                if relationship is None or predictor is None or target_bar is None or previous_bar is None:
                    continue
                prediction = relationship.intercept + relationship.slope * predictor.return_value
                if abs(prediction) < min_prediction:
                    continue
                side = "long" if prediction > 0 else "short"
                candidates.append((abs(prediction), symbol, side, prediction, target_bar, previous_bar))
            candidates.sort(reverse=True, key=lambda item: item[0])
            for _, symbol, side, prediction, target_bar, previous_bar in candidates[:3]:
                if day_trade_counts[day] >= 6:
                    break
                trade = simulate_trade(symbol, side, prediction, target_bar, previous_bar, balance)
                if trade is not None:
                    trades.append(trade)
                    balance += trade["net_pnl"]
                    day_trade_counts[day] += 1

    days = (end_ms - start_ms) / DAY_MS
    metrics = summarize(trades, initial_balance, balance, days)
    metrics["model_periods"] = len(models_by_period)
    return metrics, trades


def compact(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "model_periods"}


def main() -> int:
    args = parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    symbols = list(config["market"]["symbols"])
    histories = load_histories(Path(args.cache_dir), symbols)
    hourly = {symbol: aggregate_hours(candles) for symbol, candles in histories.items()}
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    audit_start = data_end - 420 * DAY_MS
    validation_start = audit_start + 90 * DAY_MS
    holdout_start = audit_start + 360 * DAY_MS
    holdout_end = audit_start + 420 * DAY_MS

    report: dict[str, Any] = {
        "method": {
            "training_days": 90,
            "relationship_selection": "fit first 45d, require positive edge on next 45d, refit full 90d",
            "rebalance_days": 30,
            "entry": "target UTC hour open, using only earlier closed hours from the same UTC day",
            "exit": "1 ATR stop, 2 ATR target, or target hour close",
            "costs_bps": {"market_fee_each_side": 5.0, "slippage_each_side": 2.0},
            "limits": {"max_per_hour": 3, "max_per_day": 6},
            "symbols": symbols,
        },
        "dates": {
            "validation_start": dt.datetime.fromtimestamp(validation_start / 1000, dt.timezone.utc).isoformat(),
            "holdout_start": dt.datetime.fromtimestamp(holdout_start / 1000, dt.timezone.utc).isoformat(),
            "holdout_end": dt.datetime.fromtimestamp(holdout_end / 1000, dt.timezone.utc).isoformat(),
        },
    }

    if args.holdout_config:
        selected = json.loads(args.holdout_config)
        metrics, trades = run_walk_forward(
            hourly,
            symbols,
            holdout_start,
            holdout_end,
            float(selected["min_prediction"]),
            float(selected["min_eval_win_rate"]),
        )
        report["selected_config"] = selected
        report["final_holdout_60d"] = metrics
        report["trade_log"] = trades
        print(json.dumps({"selected_config": selected, "final_holdout_60d": compact(metrics)}, indent=2))
    else:
        grid: list[dict[str, Any]] = []
        for min_eval_win_rate in (0.50, 0.53, 0.56):
            for min_prediction in (0.0002, 0.0004, 0.0006, 0.0008):
                metrics, _ = run_walk_forward(
                    hourly,
                    symbols,
                    validation_start,
                    holdout_start,
                    min_prediction,
                    min_eval_win_rate,
                )
                row = {
                    "min_prediction": min_prediction,
                    "min_eval_win_rate": min_eval_win_rate,
                    **compact(metrics),
                }
                grid.append(row)
                print(json.dumps(row, sort_keys=True), flush=True)
        report["validation_grid_270d"] = grid

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"saved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
