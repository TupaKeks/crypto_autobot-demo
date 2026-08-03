#!/usr/bin/env python3
"""No-lookahead linear intraday model research.

This file is research-only and intentionally uses NumPy.  It trains on the
previous 120 days, re-fits every 30 days, and leaves the last 60 days untouched
unless a single selected configuration is supplied with --holdout-config.
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
from typing import Any

try:
    import numpy as np
except ImportError as exc:
    raise SystemExit("NumPy is required for this research script.") from exc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import Candle
from strategy_intraday import adx, atr, ema, rsi, sma


DAY_MS = 86_400_000
BAR_MS = 900_000


@dataclasses.dataclass(frozen=True)
class Model:
    mean: np.ndarray
    scale: np.ndarray
    coefficients: np.ndarray
    threshold_by_quantile: dict[float, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.demo.regime-scalp.example.json")
    parser.add_argument("--cache-dir", default="data/market_cache_15m_430d")
    parser.add_argument("--output", default="research/linear_ml_walkforward.json")
    parser.add_argument(
        "--holdout-config",
        help="JSON such as '{\"horizon_bars\":8,\"signal_quantile\":0.995}'",
    )
    return parser.parse_args()


def load_histories(cache_dir: Path, symbols: list[str]) -> dict[str, list[Candle]]:
    result: dict[str, list[Candle]] = {}
    for symbol in symbols:
        matches = sorted(cache_dir.glob(f"{symbol}-15m-*.json"))
        if not matches:
            raise FileNotFoundError(f"No cached 15m history for {symbol}")
        result[symbol] = [Candle(**row) for row in json.loads(matches[-1].read_text())]
    return result


def safe_return(current: float, previous: float) -> float:
    return math.log(current / previous) if current > 0 and previous > 0 else 0.0


def build_feature_rows(
    candles: list[Candle],
    btc_by_time: dict[int, Candle],
    symbol_index: int,
    symbol_count: int,
) -> list[list[float] | None]:
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema48 = ema(closes, 48)
    ema144 = ema(closes, 144)
    atr14 = atr(candles, 14)
    rsi14 = rsi(closes, 14)
    adx14 = adx(candles, 14)
    volume20 = sma(volumes, 20)
    btc_times = sorted(btc_by_time)
    btc_candles = [btc_by_time[timestamp] for timestamp in btc_times]
    btc_index = {timestamp: index for index, timestamp in enumerate(btc_times)}
    btc_closes = [candle.close for candle in btc_candles]
    btc_ema9 = ema(btc_closes, 9)
    btc_ema21 = ema(btc_closes, 21)
    btc_atr = atr(btc_candles, 14)

    rows: list[list[float] | None] = [None] * len(candles)
    for index, candle in enumerate(candles):
        if index < 160:
            continue
        indicator_values = (
            ema9[index], ema21[index], ema48[index], ema144[index], atr14[index],
            rsi14[index], adx14[index], volume20[index],
        )
        btc_i = btc_index.get(candle.open_time)
        if any(value is None for value in indicator_values) or btc_i is None or btc_i < 16:
            continue
        if btc_ema9[btc_i] is None or btc_ema21[btc_i] is None or btc_atr[btc_i] in (None, 0):
            continue
        atr_value = float(atr14[index])
        if atr_value <= 0 or float(volume20[index]) <= 0:
            continue
        candle_range = max(candle.high - candle.low, 1e-12)
        timestamp = dt.datetime.fromtimestamp(candle.open_time / 1000, dt.timezone.utc)
        row = [
            safe_return(closes[index], closes[index - 1]),
            safe_return(closes[index], closes[index - 2]),
            safe_return(closes[index], closes[index - 4]),
            safe_return(closes[index], closes[index - 8]),
            safe_return(closes[index], closes[index - 16]),
            (candle.close - float(ema9[index])) / atr_value,
            (float(ema9[index]) - float(ema21[index])) / atr_value,
            (float(ema48[index]) - float(ema144[index])) / atr_value,
            atr_value / candle.close,
            float(rsi14[index]) / 100.0 - 0.5,
            float(adx14[index]) / 100.0,
            candle.volume / float(volume20[index]) - 1.0,
            (candle.close - candle.open) / candle_range,
            safe_return(btc_closes[btc_i], btc_closes[btc_i - 1]),
            safe_return(btc_closes[btc_i], btc_closes[btc_i - 4]),
            safe_return(btc_closes[btc_i], btc_closes[btc_i - 16]),
            (float(btc_ema9[btc_i]) - float(btc_ema21[btc_i])) / float(btc_atr[btc_i]),
            math.sin(timestamp.hour * 2 * math.pi / 24),
            math.cos(timestamp.hour * 2 * math.pi / 24),
        ]
        row.extend(1.0 if item == symbol_index else 0.0 for item in range(symbol_count))
        rows[index] = row
    return rows


def fit_model(
    histories: dict[str, list[Candle]],
    features: dict[str, list[list[float] | None]],
    train_start: int,
    train_end: int,
    horizon: int,
    quantiles: tuple[float, ...],
) -> Model | None:
    x_rows: list[list[float]] = []
    targets: list[float] = []
    for symbol, candles in histories.items():
        rows = features[symbol]
        for index in range(0, len(candles) - horizon - 1, 2):
            candle = candles[index]
            if not train_start <= candle.open_time < train_end - horizon * BAR_MS:
                continue
            row = rows[index]
            entry = candles[index + 1].open
            future = candles[index + horizon].close
            if row is None or entry <= 0 or future <= 0:
                continue
            x_rows.append(row)
            targets.append(math.log(future / entry))
    if len(x_rows) < 5_000:
        return None
    matrix = np.asarray(x_rows, dtype=float)
    target = np.asarray(targets, dtype=float)
    mean = matrix.mean(axis=0)
    scale = matrix.std(axis=0)
    scale[scale < 1e-10] = 1.0
    normalized = (matrix - mean) / scale
    normalized = np.column_stack((np.ones(len(normalized)), normalized))
    penalty = np.eye(normalized.shape[1]) * 10.0
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(normalized.T @ normalized + penalty, normalized.T @ target)
    predictions = normalized @ coefficients
    thresholds = {quantile: float(np.quantile(np.abs(predictions), quantile)) for quantile in quantiles}
    return Model(mean, scale, coefficients, thresholds)


def predict(model: Model, row: list[float]) -> float:
    normalized = (np.asarray(row, dtype=float) - model.mean) / model.scale
    return float(model.coefficients[0] + normalized @ model.coefficients[1:])


def slipped(price: float, order_side: str, bps: float = 2.0) -> float:
    return price * (1 + bps / 10_000 if order_side == "buy" else 1 - bps / 10_000)


def run_portfolio(
    histories: dict[str, list[Candle]],
    features: dict[str, list[list[float] | None]],
    models: dict[int, Model],
    start_ms: int,
    end_ms: int,
    horizon: int,
    quantile: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_time = {symbol: {candle.open_time: index for index, candle in enumerate(candles)} for symbol, candles in histories.items()}
    timestamps = sorted({timestamp for indexes in by_time.values() for timestamp in indexes if start_ms <= timestamp < end_ms})
    pending: dict[str, dict[str, Any]] = {}
    positions: dict[str, dict[str, Any]] = {}
    last_exit: dict[str, int] = {}
    daily_count: dict[str, int] = defaultdict(int)
    trades: list[dict[str, Any]] = []
    initial_balance = 10_000.0
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0

    for timestamp in timestamps:
        day = dt.datetime.fromtimestamp(timestamp / 1000, dt.timezone.utc).date().isoformat()
        for symbol, order in sorted(list(pending.items()), key=lambda item: item[1]["strength"], reverse=True):
            index = by_time[symbol].get(timestamp)
            if index is None or index != order["signal_index"] + 1:
                continue
            pending.pop(symbol)
            if len(positions) >= 3 or daily_count[day] >= 6:
                continue
            candle = histories[symbol][index]
            entry_side = "buy" if order["side"] == "long" else "sell"
            entry = slipped(candle.open, entry_side)
            stop_distance = order["atr"] * 1.5
            target_distance = order["atr"] * 3.0
            risk_cash = balance * 0.15 / 100
            quantity = min(risk_cash / stop_distance, balance * 2 * 0.95 / entry)
            if quantity <= 0:
                continue
            stop = entry - stop_distance if order["side"] == "long" else entry + stop_distance
            target = entry + target_distance if order["side"] == "long" else entry - target_distance
            entry_fee = entry * quantity * 0.0005
            balance -= entry_fee
            positions[symbol] = {
                "symbol": symbol,
                "side": order["side"],
                "entry": entry,
                "stop": stop,
                "target": target,
                "quantity": quantity,
                "entry_fee": entry_fee,
                "risk_cash": risk_cash,
                "entry_time": timestamp,
                "entry_index": index,
                "bars": 0,
                "prediction": order["prediction"],
            }
            daily_count[day] += 1

        for symbol, position in list(positions.items()):
            index = by_time[symbol].get(timestamp)
            if index is None or index < position["entry_index"]:
                continue
            candle = histories[symbol][index]
            position["bars"] += 1
            if position["side"] == "long":
                stop_hit = candle.low <= position["stop"]
                target_hit = candle.high >= position["target"]
            else:
                stop_hit = candle.high >= position["stop"]
                target_hit = candle.low <= position["target"]
            raw_exit = None
            reason = ""
            if stop_hit:
                raw_exit, reason = position["stop"], "stop"
            elif target_hit:
                raw_exit, reason = position["target"], "target"
            elif position["bars"] >= horizon:
                raw_exit, reason = candle.close, "time"
            if raw_exit is None:
                continue
            exit_side = "sell" if position["side"] == "long" else "buy"
            exit_price = slipped(raw_exit, exit_side)
            quantity = position["quantity"]
            gross = (exit_price - position["entry"]) * quantity
            if position["side"] == "short":
                gross = -gross
            exit_fee = exit_price * quantity * 0.0005
            net = gross - position["entry_fee"] - exit_fee
            balance += gross - exit_fee
            trade = {
                "symbol": symbol,
                "side": position["side"],
                "entry_time": position["entry_time"],
                "exit_time": timestamp,
                "net_pnl": round(net, 4),
                "realized_r": round(net / position["risk_cash"], 4),
                "reason": reason,
                "prediction": round(position["prediction"], 8),
            }
            trades.append(trade)
            positions.pop(symbol)
            last_exit[symbol] = index
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, (peak - balance) / peak * 100)

        period = max(0, (timestamp - start_ms) // (30 * DAY_MS))
        model = models.get(period)
        if model is None or daily_count[day] >= 6:
            continue
        threshold = model.threshold_by_quantile[quantile]
        candidates: list[tuple[float, str, int, float, float]] = []
        for symbol, candles in histories.items():
            if symbol in positions or symbol in pending:
                continue
            index = by_time[symbol].get(timestamp)
            if index is None or index + 1 >= len(candles) or index - last_exit.get(symbol, -10_000) <= 3:
                continue
            row = features[symbol][index]
            atr_values = atr(candles[max(0, index - 20) : index + 1], 14)
            atr_value = atr_values[-1] if atr_values else None
            if row is None or atr_value is None:
                continue
            prediction = predict(model, row)
            if abs(prediction) < threshold:
                continue
            candidates.append((abs(prediction), symbol, index, prediction, float(atr_value)))
        free_slots = max(0, 3 - len(positions) - len(pending))
        for strength, symbol, index, prediction, atr_value in sorted(candidates, reverse=True)[:free_slots]:
            pending[symbol] = {
                "signal_index": index,
                "side": "long" if prediction > 0 else "short",
                "prediction": prediction,
                "strength": strength,
                "atr": atr_value,
            }

    winners = [trade for trade in trades if trade["net_pnl"] > 0]
    losers = [trade for trade in trades if trade["net_pnl"] < 0]
    gross_profit = sum(trade["net_pnl"] for trade in winners)
    gross_loss = abs(sum(trade["net_pnl"] for trade in losers))
    days = (end_ms - start_ms) / DAY_MS
    metrics = {
        "trades": len(trades),
        "trades_per_day": round(len(trades) / days, 2),
        "win_rate": round(len(winners) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "average_realized_r": round(sum(item["realized_r"] for item in trades) / len(trades), 4) if trades else 0.0,
        "return_percent": round((balance / initial_balance - 1) * 100, 2),
        "max_drawdown_percent": round(max_drawdown, 2),
    }
    return metrics, trades


def build_models(
    histories: dict[str, list[Candle]],
    features: dict[str, list[list[float] | None]],
    start_ms: int,
    end_ms: int,
    horizon: int,
    quantiles: tuple[float, ...],
) -> dict[int, Model]:
    periods = math.ceil((end_ms - start_ms) / (30 * DAY_MS))
    models: dict[int, Model] = {}
    for period in range(periods):
        period_start = start_ms + period * 30 * DAY_MS
        model = fit_model(
            histories,
            features,
            period_start - 120 * DAY_MS,
            period_start,
            horizon,
            quantiles,
        )
        if model is not None:
            models[period] = model
    return models


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    previous_report: dict[str, Any] = {}
    if output.exists():
        try:
            previous_report = json.loads(output.read_text())
        except (json.JSONDecodeError, OSError):
            previous_report = {}
    config = json.loads(Path(args.config).read_text())
    symbols = list(config["market"]["symbols"])
    histories = load_histories(Path(args.cache_dir), symbols)
    btc_history = load_histories(Path(args.cache_dir), ["BTCUSDT"])["BTCUSDT"]
    btc_by_time = {candle.open_time: candle for candle in btc_history}
    features = {
        symbol: build_feature_rows(candles, btc_by_time, index, len(symbols))
        for index, (symbol, candles) in enumerate(histories.items())
    }
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    audit_start = data_end - 420 * DAY_MS
    validation_start = audit_start + 120 * DAY_MS
    holdout_start = audit_start + 360 * DAY_MS
    holdout_end = audit_start + 420 * DAY_MS
    quantiles = (0.99, 0.9925, 0.995)
    report: dict[str, Any] = {
        "method": {
            "model": "pooled ridge regression with symbol fixed effects",
            "training_days": 120,
            "rebalance_days": 30,
            "features": "price returns, EMA distances, ATR, RSI, ADX, volume, candle body, BTC regime, UTC hour",
            "execution": "next 15m open; 1.5 ATR stop; 3 ATR target; stop-first; market costs",
            "costs_bps": {"fee_each_side": 5.0, "slippage_each_side": 2.0},
        },
        "dates": {
            "validation_start": dt.datetime.fromtimestamp(validation_start / 1000, dt.timezone.utc).isoformat(),
            "holdout_start": dt.datetime.fromtimestamp(holdout_start / 1000, dt.timezone.utc).isoformat(),
            "holdout_end": dt.datetime.fromtimestamp(holdout_end / 1000, dt.timezone.utc).isoformat(),
        },
    }

    if args.holdout_config:
        selected = json.loads(args.holdout_config)
        horizon = int(selected["horizon_bars"])
        quantile = float(selected["signal_quantile"])
        models = build_models(histories, features, holdout_start, holdout_end, horizon, (quantile,))
        metrics, trades = run_portfolio(histories, features, models, holdout_start, holdout_end, horizon, quantile)
        report["selected_config"] = selected
        report["final_holdout_60d"] = metrics
        report["trade_log"] = trades
        if "validation_grid_240d" in previous_report:
            report["validation_grid_240d"] = previous_report["validation_grid_240d"]
        print(json.dumps({"selected_config": selected, "final_holdout_60d": metrics}, indent=2))
    else:
        rows: list[dict[str, Any]] = []
        for horizon in (4, 8, 16):
            models = build_models(histories, features, validation_start, holdout_start, horizon, quantiles)
            for quantile in quantiles:
                metrics, _ = run_portfolio(
                    histories, features, models, validation_start, holdout_start, horizon, quantile
                )
                row = {"horizon_bars": horizon, "signal_quantile": quantile, **metrics}
                rows.append(row)
                print(json.dumps(row, sort_keys=True), flush=True)
        report["validation_grid_240d"] = rows
        for key in ("selected_config", "final_holdout_60d", "trade_log"):
            if key in previous_report:
                report[key] = previous_report[key]

    output.write_text(json.dumps(report, indent=2))
    print(f"saved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
