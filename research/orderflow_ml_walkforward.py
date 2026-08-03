#!/usr/bin/env python3
"""Walk-forward nonlinear order-flow model for 15-minute Binance Futures data.

Research dependency: scikit-learn.  The model is trained on 90 days, its entry
threshold is calibrated on the following 30 days, and it then trades the next
30 days.  The final 60 days are opened only with --holdout-config.
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
    from sklearn.ensemble import HistGradientBoostingRegressor
except ImportError as exc:
    raise SystemExit("Install the research dependency: python3 -m pip install scikit-learn") from exc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import Candle
from strategy_intraday import adx, atr, ema, rsi, sma, taker_imbalance


DAY_MS = 86_400_000
BAR_MS = 900_000
QUANTILES = (0.99, 0.995, 0.9975)


@dataclasses.dataclass(frozen=True)
class Outcome:
    realized_r: float
    exit_time: int
    reason: str


@dataclasses.dataclass(frozen=True)
class PeriodModel:
    estimator: Any
    thresholds: dict[float, float]
    calibration: dict[float, dict[str, float]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.demo.regime-scalp.example.json")
    parser.add_argument("--cache-dir", default="data/market_cache_orderflow_15m_430d")
    parser.add_argument("--output", default="research/orderflow_ml_walkforward.json")
    parser.add_argument(
        "--holdout-config",
        help="JSON such as '{\"signal_quantile\":0.9975}'",
    )
    return parser.parse_args()


def load_histories(cache_dir: Path, symbols: list[str]) -> dict[str, list[Candle]]:
    histories: dict[str, list[Candle]] = {}
    for symbol in symbols:
        matches = sorted(cache_dir.glob(f"{symbol}-15m-*.json"))
        if not matches:
            raise FileNotFoundError(f"No extended 15m cache for {symbol}")
        histories[symbol] = [Candle(**row) for row in json.loads(matches[-1].read_text())]
    return histories


def log_return(current: float, previous: float) -> float:
    return math.log(current / previous) if current > 0 and previous > 0 else 0.0


def rolling_average(values: list[float], length: int) -> list[float | None]:
    return sma(values, length)


def build_base_features(
    candles: list[Candle],
    btc_by_time: dict[int, Candle],
    symbol_index: int,
    symbol_count: int,
) -> tuple[list[list[float] | None], list[float | None]]:
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    trade_counts = [float(candle.trade_count) for candle in candles]
    imbalances = [float(taker_imbalance(candle) or 0.0) for candle in candles]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema48 = ema(closes, 48)
    ema144 = ema(closes, 144)
    atr14 = atr(candles, 14)
    rsi14 = rsi(closes, 14)
    adx14 = adx(candles, 14)
    volume20 = sma(volumes, 20)
    trades20 = sma(trade_counts, 20)
    imbalance3 = rolling_average(imbalances, 3)
    imbalance12 = rolling_average(imbalances, 12)

    btc_times = sorted(btc_by_time)
    btc_candles = [btc_by_time[timestamp] for timestamp in btc_times]
    btc_index = {timestamp: index for index, timestamp in enumerate(btc_times)}
    btc_closes = [candle.close for candle in btc_candles]
    btc_imbalances = [float(taker_imbalance(candle) or 0.0) for candle in btc_candles]
    btc_ema9 = ema(btc_closes, 9)
    btc_ema21 = ema(btc_closes, 21)
    btc_atr14 = atr(btc_candles, 14)
    btc_flow3 = rolling_average(btc_imbalances, 3)

    rows: list[list[float] | None] = [None] * len(candles)
    for index, candle in enumerate(candles):
        if index < 160:
            continue
        btc_i = btc_index.get(candle.open_time)
        values = (
            ema9[index], ema21[index], ema48[index], ema144[index], atr14[index],
            rsi14[index], adx14[index], volume20[index], trades20[index],
            imbalance3[index], imbalance12[index],
        )
        if btc_i is None or btc_i < 32 or any(value is None for value in values):
            continue
        if btc_ema9[btc_i] is None or btc_ema21[btc_i] is None or btc_atr14[btc_i] in (None, 0):
            continue
        atr_value = float(atr14[index])
        if atr_value <= 0 or float(volume20[index]) <= 0 or float(trades20[index]) <= 0:
            continue
        candle_range = max(candle.high - candle.low, 1e-12)
        timestamp = dt.datetime.fromtimestamp(candle.open_time / 1000, dt.timezone.utc)
        signed = [
            log_return(closes[index], closes[index - 1]),
            log_return(closes[index], closes[index - 2]),
            log_return(closes[index], closes[index - 4]),
            log_return(closes[index], closes[index - 8]),
            log_return(closes[index], closes[index - 16]),
            log_return(closes[index], closes[index - 32]),
            (candle.close - float(ema9[index])) / atr_value,
            (float(ema9[index]) - float(ema21[index])) / atr_value,
            (float(ema48[index]) - float(ema144[index])) / atr_value,
            float(rsi14[index]) / 100.0 - 0.5,
            (candle.close - candle.open) / candle_range,
            imbalances[index],
            float(imbalance3[index]),
            float(imbalance12[index]),
            imbalances[index] - imbalances[index - 1],
            log_return(btc_closes[btc_i], btc_closes[btc_i - 1]),
            log_return(btc_closes[btc_i], btc_closes[btc_i - 4]),
            log_return(btc_closes[btc_i], btc_closes[btc_i - 16]),
            (float(btc_ema9[btc_i]) - float(btc_ema21[btc_i])) / float(btc_atr14[btc_i]),
            btc_imbalances[btc_i],
            float(btc_flow3[btc_i] or 0.0),
        ]
        unsigned = [
            atr_value / candle.close,
            float(adx14[index]) / 100.0,
            min(candle.volume / float(volume20[index]), 10.0),
            min(candle.trade_count / float(trades20[index]), 10.0),
            abs(imbalances[index]),
            abs(float(imbalance3[index])),
            (candle.high - candle.low) / atr_value,
            math.sin(timestamp.hour * 2 * math.pi / 24),
            math.cos(timestamp.hour * 2 * math.pi / 24),
            math.sin(timestamp.weekday() * 2 * math.pi / 7),
            math.cos(timestamp.weekday() * 2 * math.pi / 7),
        ]
        one_hot = [1.0 if item == symbol_index else 0.0 for item in range(symbol_count)]
        rows[index] = signed + unsigned + one_hot
    return rows, atr14


def directional_features(base: list[float], direction: int, symbol_count: int) -> list[float]:
    signed_count = 21
    unsigned_count = 11
    return (
        [direction * value for value in base[:signed_count]]
        + base[signed_count : signed_count + unsigned_count]
        + base[-symbol_count:]
    )


def trade_outcome(
    candles: list[Candle],
    signal_index: int,
    direction: int,
    atr_value: float,
    horizon: int = 16,
    fee_bps: float = 5.0,
    maker_fee_bps: float = 2.0,
    slippage_bps: float = 2.0,
) -> Outcome | None:
    if signal_index + horizon >= len(candles):
        return None
    entry_raw = candles[signal_index + 1].open
    entry = entry_raw * (1 + direction * slippage_bps / 10_000)
    stop_distance = atr_value * 1.5
    if entry <= 0 or stop_distance <= 0:
        return None
    stop = entry - direction * stop_distance
    target = entry + direction * stop_distance * 2.0
    raw_exit = candles[signal_index + horizon].close
    exit_time = candles[signal_index + horizon].close_time
    reason = "time"
    for candle in candles[signal_index + 1 : signal_index + horizon + 1]:
        stop_hit = candle.low <= stop if direction > 0 else candle.high >= stop
        target_hit = candle.high >= target if direction > 0 else candle.low <= target
        if stop_hit:
            raw_exit = stop
            exit_time = candle.close_time
            reason = "stop"
            break
        if target_hit:
            raw_exit = target
            exit_time = candle.close_time
            reason = "target"
            break
    target_fill = reason == "target"
    exit_slippage = 0.0 if target_fill else slippage_bps
    exit_fee_bps = maker_fee_bps if target_fill else fee_bps
    exit_price = raw_exit * (1 - direction * exit_slippage / 10_000)
    gross_r = direction * (exit_price - entry) / stop_distance
    fees_r = (
        entry * fee_bps / 10_000 + exit_price * exit_fee_bps / 10_000
    ) / stop_distance
    return Outcome(gross_r - fees_r, exit_time, reason)


def build_samples(
    histories: dict[str, list[Candle]],
    features: dict[str, list[list[float] | None]],
    atr_values: dict[str, list[float | None]],
    symbol_count: int,
    start_ms: int,
    end_ms: int,
    step: int,
) -> tuple[np.ndarray, np.ndarray]:
    x_rows: list[list[float]] = []
    targets: list[float] = []
    for symbol, candles in histories.items():
        for index in range(160, len(candles) - 16, step):
            candle = candles[index]
            if not start_ms <= candle.open_time < end_ms - 16 * BAR_MS:
                continue
            base = features[symbol][index]
            atr_value = atr_values[symbol][index]
            if base is None or atr_value is None:
                continue
            for direction in (1, -1):
                outcome = trade_outcome(candles, index, direction, float(atr_value))
                if outcome is None:
                    continue
                x_rows.append(directional_features(base, direction, symbol_count))
                targets.append(outcome.realized_r)
    return np.asarray(x_rows, dtype=np.float32), np.asarray(targets, dtype=np.float32)


def fit_period_model(
    histories: dict[str, list[Candle]],
    features: dict[str, list[list[float] | None]],
    atr_values: dict[str, list[float | None]],
    symbol_count: int,
    period_start: int,
    quantiles: tuple[float, ...],
) -> PeriodModel | None:
    train_x, train_y = build_samples(
        histories,
        features,
        atr_values,
        symbol_count,
        period_start - 120 * DAY_MS,
        period_start - 30 * DAY_MS,
        step=2,
    )
    if len(train_y) < 20_000:
        return None
    estimator = HistGradientBoostingRegressor(
        learning_rate=0.05,
        max_iter=120,
        max_leaf_nodes=15,
        max_depth=4,
        min_samples_leaf=150,
        l2_regularization=2.0,
        random_state=17,
    )
    estimator.fit(train_x, train_y)
    calibration_x, calibration_y = build_samples(
        histories,
        features,
        atr_values,
        symbol_count,
        period_start - 30 * DAY_MS,
        period_start,
        step=1,
    )
    predictions = estimator.predict(calibration_x)
    thresholds: dict[float, float] = {}
    calibration: dict[float, dict[str, float]] = {}
    for quantile in quantiles:
        threshold = float(np.quantile(predictions, quantile))
        selected = calibration_y[predictions >= threshold]
        thresholds[quantile] = threshold
        calibration[quantile] = {
            "signals": int(len(selected)),
            "average_r": round(float(selected.mean()), 5) if len(selected) else 0.0,
            "win_rate": round(float((selected > 0).mean() * 100), 2) if len(selected) else 0.0,
        }
    return PeriodModel(estimator, thresholds, calibration)


def build_models(
    histories: dict[str, list[Candle]],
    features: dict[str, list[list[float] | None]],
    atr_values: dict[str, list[float | None]],
    start_ms: int,
    end_ms: int,
    quantiles: tuple[float, ...],
) -> dict[int, PeriodModel]:
    periods = math.ceil((end_ms - start_ms) / (30 * DAY_MS))
    models: dict[int, PeriodModel] = {}
    for period in range(periods):
        print(f"fitting period {period + 1}/{periods}", flush=True)
        period_start = start_ms + period * 30 * DAY_MS
        model = fit_period_model(
            histories,
            features,
            atr_values,
            len(histories),
            period_start,
            quantiles,
        )
        if model is not None:
            models[period] = model
    return models


def precompute_scores(
    histories: dict[str, list[Candle]],
    features: dict[str, list[list[float] | None]],
    atr_values: dict[str, list[float | None]],
    models: dict[int, PeriodModel],
    start_ms: int,
    end_ms: int,
) -> dict[int, list[tuple[float, str, int, int, float]]]:
    """Batch model inference once so portfolio variants reuse identical scores."""
    result: dict[int, list[tuple[float, str, int, int, float]]] = defaultdict(list)
    for period, model in models.items():
        period_start = start_ms + period * 30 * DAY_MS
        period_end = min(period_start + 30 * DAY_MS, end_ms)
        rows: list[list[float]] = []
        metadata: list[tuple[str, int, int, float]] = []
        for symbol, candles in histories.items():
            for index, candle in enumerate(candles):
                if not period_start <= candle.open_time < period_end or index + 16 >= len(candles):
                    continue
                base = features[symbol][index]
                atr_value = atr_values[symbol][index]
                if base is None or atr_value is None:
                    continue
                rows.append(directional_features(base, 1, len(histories)))
                metadata.append((symbol, index, 1, float(atr_value)))
                rows.append(directional_features(base, -1, len(histories)))
                metadata.append((symbol, index, -1, float(atr_value)))
        if not rows:
            continue
        predictions = model.estimator.predict(np.asarray(rows, dtype=np.float32))
        best_by_signal: dict[tuple[str, int], tuple[float, int, float]] = {}
        for prediction, (symbol, index, direction, atr_value) in zip(predictions, metadata):
            key = (symbol, index)
            current = best_by_signal.get(key)
            if current is None or float(prediction) > current[0]:
                best_by_signal[key] = (float(prediction), direction, atr_value)
        for (symbol, index), (score, direction, atr_value) in best_by_signal.items():
            timestamp = histories[symbol][index].open_time
            result[timestamp].append((score, symbol, index, direction, atr_value))
    return dict(result)


def run_portfolio(
    histories: dict[str, list[Candle]],
    features: dict[str, list[list[float] | None]],
    atr_values: dict[str, list[float | None]],
    models: dict[int, PeriodModel],
    scores_by_time: dict[int, list[tuple[float, str, int, int, float]]],
    start_ms: int,
    end_ms: int,
    quantile: float | str,
    fee_bps: float = 5.0,
    maker_fee_bps: float = 2.0,
    slippage_bps: float = 2.0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    indexes = {symbol: {candle.open_time: index for index, candle in enumerate(candles)} for symbol, candles in histories.items()}
    timestamps = sorted({timestamp for values in indexes.values() for timestamp in values if start_ms <= timestamp < end_ms})
    positions: dict[str, dict[str, Any]] = {}
    pending: dict[str, dict[str, Any]] = {}
    last_exit_index: dict[str, int] = {}
    daily_count: dict[str, int] = defaultdict(int)
    trades: list[dict[str, Any]] = []
    initial_balance = 10_000.0
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0
    disabled_periods = 0

    for timestamp in timestamps:
        day = dt.datetime.fromtimestamp(timestamp / 1000, dt.timezone.utc).date().isoformat()
        for symbol, order in list(pending.items()):
            index = indexes[symbol].get(timestamp)
            if index is None or index != order["signal_index"] + 1:
                continue
            pending.pop(symbol)
            if len(positions) >= 3 or daily_count[day] >= 6:
                continue
            outcome = trade_outcome(
                histories[symbol],
                order["signal_index"],
                order["direction"],
                order["atr"],
                fee_bps=fee_bps,
                maker_fee_bps=maker_fee_bps,
                slippage_bps=slippage_bps,
            )
            if outcome is None:
                continue
            positions[symbol] = {
                "symbol": symbol,
                "side": "long" if order["direction"] > 0 else "short",
                "entry_time": timestamp,
                "entry_index": index,
                "exit_time": outcome.exit_time,
                "realized_r": outcome.realized_r,
                "reason": outcome.reason,
                "score": order["score"],
                "risk_cash": balance * 0.15 / 100,
            }
            daily_count[day] += 1

        for symbol, position in list(positions.items()):
            if timestamp < position["exit_time"]:
                continue
            net_pnl = position["realized_r"] * position["risk_cash"]
            balance += net_pnl
            trades.append(
                {
                    "symbol": symbol,
                    "side": position["side"],
                    "entry_time": position["entry_time"],
                    "exit_time": position["exit_time"],
                    "realized_r": round(position["realized_r"], 4),
                    "net_pnl": round(net_pnl, 4),
                    "reason": position["reason"],
                    "score": round(position["score"], 5),
                }
            )
            positions.pop(symbol)
            last_exit_index[symbol] = indexes[symbol].get(timestamp, position["entry_index"])
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, (peak - balance) / peak * 100)

        period = max(0, (timestamp - start_ms) // (30 * DAY_MS))
        model = models.get(period)
        if model is None or daily_count[day] >= 6:
            continue
        selected_quantile: float | str = quantile
        if quantile == "dynamic":
            eligible_quantiles = [
                item
                for item, item_calibration in model.calibration.items()
                if item_calibration["average_r"] > 0
                and item_calibration["win_rate"] >= 45
                and item_calibration.get("profit_factor", 0.0) >= 1.1
            ]
            if not eligible_quantiles:
                disabled_periods += 1
                continue
            selected_quantile = min(eligible_quantiles)
        calibration = model.calibration[selected_quantile]
        if calibration["average_r"] <= 0 or calibration["win_rate"] < 40:
            disabled_periods += 1
            continue
        threshold = model.thresholds[selected_quantile]
        candidates: list[tuple[float, str, int, int, float]] = []
        for score, symbol, index, direction, atr_value in scores_by_time.get(timestamp, []):
            if symbol in positions or symbol in pending:
                continue
            if index - last_exit_index.get(symbol, -10_000) <= 3:
                continue
            if score >= threshold:
                candidates.append((score, symbol, index, direction, atr_value))
        free_slots = max(0, 3 - len(positions) - len(pending))
        for score, symbol, index, direction, atr_value in sorted(candidates, reverse=True)[:free_slots]:
            pending[symbol] = {
                "signal_index": index,
                "direction": direction,
                "atr": atr_value,
                "score": score,
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
        "average_realized_r": round(sum(trade["realized_r"] for trade in trades) / len(trades), 4) if trades else 0.0,
        "return_percent": round((balance / initial_balance - 1) * 100, 2),
        "max_drawdown_percent": round(max_drawdown, 2),
        "disabled_checks": disabled_periods,
    }
    return metrics, trades


def monthly_metrics(trades: list[dict[str, Any]], start_ms: int, months: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month in range(months):
        left = start_ms + month * 30 * DAY_MS
        right = left + 30 * DAY_MS
        selected = [trade for trade in trades if left <= trade["exit_time"] < right]
        winners = [trade for trade in selected if trade["net_pnl"] > 0]
        losers = [trade for trade in selected if trade["net_pnl"] < 0]
        profit = sum(trade["net_pnl"] for trade in winners)
        loss = abs(sum(trade["net_pnl"] for trade in losers))
        rows.append(
            {
                "trades": len(selected),
                "trades_per_day": round(len(selected) / 30, 2),
                "win_rate": round(len(winners) / len(selected) * 100, 2) if selected else 0.0,
                "profit_factor": round(profit / loss, 3) if loss else None,
                "net_pnl": round(sum(trade["net_pnl"] for trade in selected), 2),
            }
        )
    return rows


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    previous = json.loads(output.read_text()) if output.exists() else {}
    config = json.loads(Path(args.config).read_text())
    symbols = [
        "AAVEUSDT", "ADAUSDT", "APTUSDT", "BTCUSDT", "CRVUSDT",
        "DOGEUSDT", "ETHUSDT", "FILUSDT", "LINKUSDT", "NEARUSDT",
        "RENDERUSDT", "SOLUSDT", "TAOUSDT", "XLMUSDT", "XRPUSDT",
    ]
    histories = load_histories(Path(args.cache_dir), symbols)
    btc_by_time = {candle.open_time: candle for candle in histories["BTCUSDT"]}
    features: dict[str, list[list[float] | None]] = {}
    atr_values: dict[str, list[float | None]] = {}
    for index, symbol in enumerate(symbols):
        features[symbol], atr_values[symbol] = build_base_features(
            histories[symbol], btc_by_time, index, len(symbols)
        )
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    audit_start = data_end - 430 * DAY_MS
    validation_start = audit_start + 120 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS
    holdout_end = data_end
    report: dict[str, Any] = {
        "method": {
            "model": "HistGradientBoostingRegressor",
            "training": "90d fit + 30d threshold calibration + next 30d trade",
            "features": "price, trend, volatility, RSI, ADX, volume, trade count, taker imbalance, BTC regime, calendar",
            "trade": "next 15m open, RR 1:2, 1.5 ATR stop, max 16 bars",
            "costs_bps": {"fee_each_side": 5.0, "slippage_each_side": 2.0},
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
        quantile = float(selected["signal_quantile"])
        models = build_models(histories, features, atr_values, holdout_start, holdout_end, (quantile,))
        scores = precompute_scores(histories, features, atr_values, models, holdout_start, holdout_end)
        metrics, trades = run_portfolio(
            histories, features, atr_values, models, scores, holdout_start, holdout_end, quantile
        )
        report["selected_config"] = selected
        report["final_holdout_60d"] = metrics
        report["holdout_months"] = monthly_metrics(trades, holdout_start, 2)
        report["trade_log"] = trades
        if "validation_grid" in previous:
            report["validation_grid"] = previous["validation_grid"]
            report["validation_months"] = previous.get("validation_months", {})
        print(json.dumps({"selected_config": selected, "final_holdout_60d": metrics}, indent=2))
    else:
        models = build_models(histories, features, atr_values, validation_start, holdout_start, QUANTILES)
        print("precomputing validation scores", flush=True)
        scores = precompute_scores(histories, features, atr_values, models, validation_start, holdout_start)
        grid: list[dict[str, Any]] = []
        month_rows: dict[str, Any] = {}
        validation_month_count = math.ceil((holdout_start - validation_start) / (30 * DAY_MS))
        for quantile in QUANTILES:
            metrics, trades = run_portfolio(
                histories, features, atr_values, models, scores, validation_start, holdout_start, quantile
            )
            row = {"signal_quantile": quantile, **metrics}
            grid.append(row)
            month_rows[str(quantile)] = monthly_metrics(trades, validation_start, validation_month_count)
            print(json.dumps(row, sort_keys=True), flush=True)
        report["validation_grid"] = grid
        report["validation_months"] = month_rows
        for key in ("selected_config", "final_holdout_60d", "holdout_months", "trade_log"):
            if key in previous:
                report[key] = previous[key]
    output.write_text(json.dumps(report, indent=2))
    print(f"saved: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
