#!/usr/bin/env python3
"""Walk-forward meta-label model for cost-aware 1:2 pullback setups."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orderflow_features import build_base_features, directional_features  # noqa: E402
from portfolio_backtest import slipped  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics, load_cached_histories  # noqa: E402
from strategy_intraday import build_indicators, evaluate_strategy_signal, minimum_history  # noqa: E402


BAR_MS = 900_000
QUANTILES = (0.40, 0.50, 0.60, 0.70, 0.80, 0.90)


@dataclasses.dataclass(frozen=True)
class SetupOutcome:
    entry_time: int
    exit_time: int
    realized_r: float
    reason: str


@dataclasses.dataclass(frozen=True)
class Setup:
    symbol: str
    index: int
    signal_time: int
    side: str
    direction: int
    atr_value: float
    features: list[float]
    outcome: SetupOutcome | None


@dataclasses.dataclass(frozen=True)
class PeriodModel:
    model: HistGradientBoostingClassifier
    thresholds: dict[float, float]
    calibration: dict[float, dict[str, float]]


def simulate_outcome(
    candles: list[Any],
    signal_index: int,
    direction: int,
    atr_value: float,
    *,
    maker_fee_bps: float = 2.0,
    taker_fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    horizon: int = 16,
) -> SetupOutcome | None:
    if signal_index + horizon >= len(candles):
        return None
    signal = candles[signal_index]
    entry = signal.close - direction * atr_value * 0.1
    fill = candles[signal_index + 1]
    touched = fill.low <= entry if direction > 0 else fill.high >= entry
    if not touched:
        return None
    stop_distance = atr_value * 1.5
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
    exit_price = raw_exit if target_fill else slipped(
        raw_exit, "sell" if direction > 0 else "buy", slippage_bps
    )
    gross_r = direction * (exit_price - entry) / stop_distance
    exit_fee = maker_fee_bps if target_fill else taker_fee_bps
    fee_r = (entry * maker_fee_bps / 10_000 + exit_price * exit_fee / 10_000) / stop_distance
    return SetupOutcome(fill.open_time, exit_time, gross_r - fee_r, reason)


def build_setups(
    histories: dict[str, list[Any]],
    strategy: dict[str, Any],
) -> list[Setup]:
    symbols = list(histories)
    btc_by_time = {candle.open_time: candle for candle in histories["BTCUSDT"]}
    setups: list[Setup] = []
    for symbol_index, symbol in enumerate(symbols):
        candles = histories[symbol]
        indicators = build_indicators(candles, strategy)
        base_rows, _ = build_base_features(candles, btc_by_time, symbol_index, len(symbols))
        for index in range(minimum_history(strategy), len(candles) - 16):
            decision = evaluate_strategy_signal(candles, index, strategy, indicators)
            if not decision.side or decision.atr_value is None or base_rows[index] is None:
                continue
            direction = 1 if decision.side == "long" else -1
            row = directional_features(base_rows[index], direction, len(symbols))
            row.extend([
                float(decision.strength) / 100.0,
                1.0 if decision.side == "long" else 0.0,
            ])
            setups.append(
                Setup(
                    symbol=symbol,
                    index=index,
                    signal_time=candles[index].open_time,
                    side=decision.side,
                    direction=direction,
                    atr_value=float(decision.atr_value),
                    features=row,
                    outcome=simulate_outcome(candles, index, direction, float(decision.atr_value)),
                )
            )
    return sorted(setups, key=lambda item: (item.signal_time, item.symbol))


def metrics(outcomes: list[SetupOutcome]) -> dict[str, float]:
    values = [item.realized_r for item in outcomes]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    profit = sum(wins)
    loss = abs(sum(losses))
    return {
        "signals": len(values),
        "average_r": round(sum(values) / len(values), 5) if values else 0.0,
        "win_rate": round(len(wins) / len(values) * 100, 2) if values else 0.0,
        "profit_factor": round(profit / loss, 3) if loss else 0.0,
    }


def fit_period_model(setups: list[Setup], period_start: int) -> PeriodModel | None:
    train = [
        item for item in setups
        if period_start - 120 * DAY_MS <= item.signal_time < period_start - 30 * DAY_MS
        and item.outcome is not None
    ]
    if len(train) < 500:
        return None
    train_x = np.asarray([item.features for item in train], dtype=np.float32)
    train_y = np.asarray([item.outcome.realized_r > 0 for item in train])
    model = HistGradientBoostingClassifier(
        learning_rate=0.04,
        max_iter=140,
        max_leaf_nodes=9,
        max_depth=3,
        min_samples_leaf=35,
        l2_regularization=4.0,
        class_weight="balanced",
        random_state=41,
    )
    model.fit(train_x, train_y)
    calibration = [
        item for item in setups
        if period_start - 30 * DAY_MS <= item.signal_time < period_start
    ]
    if len(calibration) < 100:
        return None
    probabilities = model.predict_proba(
        np.asarray([item.features for item in calibration], dtype=np.float32)
    )[:, 1]
    thresholds: dict[float, float] = {}
    calibration_metrics: dict[float, dict[str, float]] = {}
    for quantile in QUANTILES:
        threshold = float(np.quantile(probabilities, quantile))
        selected = [
            item.outcome for item, probability in zip(calibration, probabilities)
            if probability >= threshold and item.outcome is not None
        ]
        thresholds[quantile] = threshold
        calibration_metrics[quantile] = metrics(selected)
    return PeriodModel(model, thresholds, calibration_metrics)


def choose_quantile(model: PeriodModel, policy: float | str) -> float | None:
    if policy != "dynamic":
        value = float(policy)
        item = model.calibration[value]
        return value if item["average_r"] > 0 and item["win_rate"] >= 42 else None
    eligible = [
        quantile for quantile, item in model.calibration.items()
        if item["signals"] >= 30
        and item["average_r"] > 0
        and item["win_rate"] >= 45
        and item["profit_factor"] >= 1.1
    ]
    return min(eligible) if eligible else None


def run_portfolio(
    setups: list[Setup],
    models: dict[int, PeriodModel],
    start: int,
    end: int,
    policy: float | str,
    *,
    risk_percent: float = 0.15,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_time: dict[int, list[Setup]] = defaultdict(list)
    for setup in setups:
        if start <= setup.signal_time < end:
            by_time[setup.signal_time].append(setup)
    active: dict[str, dict[str, Any]] = {}
    daily_count: dict[str, int] = defaultdict(int)
    daily_pnl: dict[str, float] = defaultdict(float)
    accepted: list[dict[str, Any]] = []
    balance = 10_000.0
    peak = balance
    drawdown = 0.0
    last_exit: dict[str, int] = {}

    def realize(timestamp: int) -> None:
        nonlocal balance, peak, drawdown
        for symbol, trade in sorted(list(active.items()), key=lambda item: item[1]["exit_time"]):
            if trade["exit_time"] > timestamp:
                continue
            balance += trade["net_pnl"]
            day = dt.datetime.fromtimestamp(trade["exit_time"] / 1000, dt.timezone.utc).date().isoformat()
            daily_pnl[day] += trade["net_pnl"]
            accepted.append(trade)
            active.pop(symbol)
            last_exit[symbol] = trade["exit_time"]
            peak = max(peak, balance)
            drawdown = max(drawdown, (peak - balance) / peak * 100)

    for timestamp in sorted(by_time):
        realize(timestamp)
        period = max(0, (timestamp - start) // (30 * DAY_MS))
        model = models.get(period)
        if model is None:
            continue
        quantile = choose_quantile(model, policy)
        if quantile is None:
            continue
        threshold = model.thresholds[quantile]
        rows = by_time[timestamp]
        probabilities = model.model.predict_proba(
            np.asarray([item.features for item in rows], dtype=np.float32)
        )[:, 1]
        ranked = sorted(zip(probabilities, rows), key=lambda pair: pair[0], reverse=True)
        for probability, setup in ranked:
            if probability < threshold or setup.outcome is None:
                continue
            if setup.symbol in active or setup.outcome.entry_time - last_exit.get(setup.symbol, -10**15) <= 3 * BAR_MS:
                continue
            entry_day = dt.datetime.fromtimestamp(
                setup.outcome.entry_time / 1000, dt.timezone.utc
            ).date().isoformat()
            if (
                len(active) >= 4
                or daily_count[entry_day] >= 8
                or daily_pnl[entry_day] <= -120.0
            ):
                continue
            risk_cash = balance * risk_percent / 100
            trade = {
                "symbol": setup.symbol,
                "side": setup.side,
                "entry_time": setup.outcome.entry_time,
                "exit_time": setup.outcome.exit_time,
                "realized_r": round(setup.outcome.realized_r, 4),
                "net_pnl": round(setup.outcome.realized_r * risk_cash, 4),
                "reason": setup.outcome.reason,
                "score": round(float(probability), 5),
            }
            active[setup.symbol] = trade
            daily_count[entry_day] += 1
    realize(end + 16 * BAR_MS)
    wins = [trade for trade in accepted if trade["net_pnl"] > 0]
    losses = [trade for trade in accepted if trade["net_pnl"] < 0]
    profit = sum(trade["net_pnl"] for trade in wins)
    loss = abs(sum(trade["net_pnl"] for trade in losses))
    days = max((end - start) / DAY_MS, 1 / 24)
    result = {
        "trades": len(accepted),
        "trades_per_day": round(len(accepted) / days, 2),
        "win_rate": round(len(wins) / len(accepted) * 100, 2) if accepted else 0.0,
        "profit_factor": round(profit / loss, 3) if loss else None,
        "average_realized_r": round(
            sum(trade["realized_r"] for trade in accepted) / len(accepted), 4
        ) if accepted else 0.0,
        "return_percent": round((balance / 10_000 - 1) * 100, 2),
        "max_drawdown_percent": round(drawdown, 2),
    }
    return result, accepted


def build_models(setups: list[Setup], start: int, end: int) -> dict[int, PeriodModel]:
    models = {}
    for period in range(math.ceil((end - start) / (30 * DAY_MS))):
        print(f"fit meta period {period + 1}", flush=True)
        model = fit_period_model(setups, start + period * 30 * DAY_MS)
        if model is not None:
            models[period] = model
    return models


def main() -> int:
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text())
    strategy = dict(config["strategy"])
    strategy["allow_longs"] = True
    symbols = [
        "AAVEUSDT", "ADAUSDT", "APTUSDT", "BTCUSDT", "CRVUSDT",
        "DOGEUSDT", "ETHUSDT", "FILUSDT", "LINKUSDT", "NEARUSDT",
        "RENDERUSDT", "SOLUSDT", "TAOUSDT", "XLMUSDT", "XRPUSDT",
    ]
    histories = load_cached_histories(
        ROOT / "data/market_cache_orderflow_15m_430d", symbols, "15m"
    )
    setups = build_setups(histories, strategy)
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    validation_start = data_end - 310 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS
    print(f"setups={len(setups)}", flush=True)
    models = build_models(setups, validation_start, holdout_start)
    candidates = []
    for policy in (*QUANTILES, "dynamic"):
        result, trades = run_portfolio(setups, models, validation_start, holdout_start, policy)
        months = [
            basic_metrics(trades, validation_start + month * 30 * DAY_MS, min(validation_start + (month + 1) * 30 * DAY_MS, holdout_start))
            for month in range(math.ceil((holdout_start - validation_start) / (30 * DAY_MS)))
        ]
        candidates.append({
            "policy": policy,
            "validation": result,
            "profitable_months": sum(month["net_pnl"] > 0 for month in months),
            "months": months,
        })
        print(json.dumps(candidates[-1], sort_keys=True), flush=True)
    eligible = [
        item for item in candidates
        if 3.5 <= item["validation"]["trades_per_day"] <= 6.5
        and item["validation"]["win_rate"] >= 45
        and float(item["validation"]["profit_factor"] or 0) >= 1.1
        and item["profitable_months"] >= 5
    ]
    selected = max(
        eligible or candidates,
        key=lambda item: (
            item["profitable_months"],
            float(item["validation"]["profit_factor"] or 0),
            -abs(item["validation"]["trades_per_day"] - 5),
        ),
    )
    passed = bool(eligible)
    report: dict[str, Any] = {
        "method": {
            "description": "pullback setup meta-label classifier with exact 1:2 execution",
            "training": "90d fit, 30d calibration, next 30d trade",
            "costs": "maker entry/target, taker stop/time exit, 2bps slippage",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "setups": len(setups),
        "validation_candidates": candidates,
        "selected_on_validation": selected,
        "validation_passed": passed,
        "holdout_opened": passed,
    }
    if passed:
        holdout_models = build_models(setups, holdout_start, data_end)
        holdout, holdout_trades = run_portfolio(
            setups, holdout_models, holdout_start, data_end, selected["policy"]
        )
        report["final_holdout_once"] = holdout
        report["holdout_months"] = [
            basic_metrics(holdout_trades, holdout_start + month * 30 * DAY_MS, min(holdout_start + (month + 1) * 30 * DAY_MS, data_end))
            for month in range(2)
        ]
    output = ROOT / "research/meta_label_pullback_walkforward.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "selected": selected,
        "validation_passed": passed,
        "final_holdout_once": report.get("final_holdout_once"),
        "output": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
