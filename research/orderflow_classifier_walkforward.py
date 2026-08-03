#!/usr/bin/env python3
"""Walk-forward probability model for cost-aware 1:2 intraday crypto trades."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "research") not in sys.path:
    sys.path.insert(0, str(ROOT / "research"))

import orderflow_ml_walkforward as base  # noqa: E402
from portfolio_backtest import prepare_histories, run_portfolio_backtest  # noqa: E402

QUANTILES = (0.975, 0.98, 0.985, 0.99, 0.995)
POLICIES: tuple[float | str, ...] = QUANTILES + ("dynamic",)


@dataclasses.dataclass
class ProbabilityEstimator:
    model: HistGradientBoostingClassifier

    def predict(self, rows: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(rows)[:, 1]


def fit_period_model(
    histories: dict[str, list[Any]],
    features: dict[str, list[list[float] | None]],
    atr_values: dict[str, list[float | None]],
    symbol_count: int,
    period_start: int,
) -> base.PeriodModel | None:
    train_x, train_r = base.build_samples(
        histories,
        features,
        atr_values,
        symbol_count,
        period_start - 120 * base.DAY_MS,
        period_start - 30 * base.DAY_MS,
        step=2,
    )
    if len(train_r) < 20_000:
        return None
    labels = train_r > 0.0
    model = HistGradientBoostingClassifier(
        learning_rate=0.04,
        max_iter=180,
        max_leaf_nodes=15,
        max_depth=4,
        min_samples_leaf=180,
        l2_regularization=3.0,
        class_weight="balanced",
        random_state=29,
    )
    model.fit(train_x, labels)
    estimator = ProbabilityEstimator(model)
    calibration_x, calibration_r = base.build_samples(
        histories,
        features,
        atr_values,
        symbol_count,
        period_start - 30 * base.DAY_MS,
        period_start,
        step=1,
    )
    probabilities = estimator.predict(calibration_x)
    thresholds: dict[float, float] = {}
    calibration: dict[float, dict[str, float]] = {}
    for quantile in QUANTILES:
        threshold = float(np.quantile(probabilities, quantile))
        selected = calibration_r[probabilities >= threshold]
        winners = selected[selected > 0]
        losers = selected[selected < 0]
        profit = float(winners.sum()) if len(winners) else 0.0
        loss = abs(float(losers.sum())) if len(losers) else 0.0
        thresholds[quantile] = threshold
        calibration[quantile] = {
            "signals": int(len(selected)),
            "average_r": round(float(selected.mean()), 5) if len(selected) else 0.0,
            "win_rate": round(float((selected > 0).mean() * 100), 2) if len(selected) else 0.0,
            "profit_factor": round(profit / loss, 3) if loss else 0.0,
        }
    return base.PeriodModel(estimator, thresholds, calibration)


def build_models(
    histories: dict[str, list[Any]],
    features: dict[str, list[list[float] | None]],
    atr_values: dict[str, list[float | None]],
    start_ms: int,
    end_ms: int,
) -> dict[int, base.PeriodModel]:
    periods = math.ceil((end_ms - start_ms) / (30 * base.DAY_MS))
    models: dict[int, base.PeriodModel] = {}
    for period in range(periods):
        print(f"fitting classifier period {period + 1}/{periods}", flush=True)
        model = fit_period_model(
            histories,
            features,
            atr_values,
            len(histories),
            start_ms + period * 30 * base.DAY_MS,
        )
        if model is not None:
            models[period] = model
    return models


def profitable_months(trades: list[dict[str, Any]], start_ms: int, count: int) -> int:
    return sum(
        float(item["net_pnl"]) > 0
        for item in base.monthly_metrics(trades, start_ms, count)
    )


def compact(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: metrics.get(key) for key in (
        "trades", "trades_per_day", "win_rate", "profit_factor",
        "average_realized_r", "return_percent", "max_drawdown_percent",
    )}


def merge_portfolio(
    baseline_trades: list[dict[str, Any]],
    classifier_trades: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates = [
        {**trade, "source": "baseline"} for trade in baseline_trades
    ] + [
        {**trade, "source": "classifier"} for trade in classifier_trades
    ]
    candidates.sort(
        key=lambda trade: (
            int(trade["entry_time"]),
            0 if trade["source"] == "baseline" else 1,
        )
    )
    active: dict[str, dict[str, Any]] = {}
    accepted: list[dict[str, Any]] = []
    daily_count: dict[str, int] = {}
    daily_pnl: dict[str, float] = {}
    balance = 10_000.0

    def realize_until(timestamp: int) -> None:
        nonlocal balance
        closing = sorted(
            (trade for trade in active.values() if int(trade["exit_time"]) <= timestamp),
            key=lambda trade: int(trade["exit_time"]),
        )
        for trade in closing:
            balance += float(trade["net_pnl"])
            exit_day = dt.datetime.fromtimestamp(
                int(trade["exit_time"]) / 1000, dt.timezone.utc
            ).date().isoformat()
            daily_pnl[exit_day] = daily_pnl.get(exit_day, 0.0) + float(trade["net_pnl"])
            active.pop(str(trade["symbol"]), None)

    for trade in candidates:
        entry_time = int(trade["entry_time"])
        realize_until(entry_time)
        symbol = str(trade["symbol"])
        day = dt.datetime.fromtimestamp(entry_time / 1000, dt.timezone.utc).date().isoformat()
        if (
            symbol in active
            or len(active) >= 4
            or daily_count.get(day, 0) >= 8
            or daily_pnl.get(day, 0.0) <= -120.0
        ):
            continue
        risk_cash = balance * 0.15 / 100
        merged = {
            **trade,
            "risk_cash": round(risk_cash, 4),
            "net_pnl": round(float(trade["realized_r"]) * risk_cash, 4),
        }
        active[symbol] = merged
        accepted.append(merged)
        daily_count[day] = daily_count.get(day, 0) + 1
    realize_until(end_ms + 1)

    winners = [trade for trade in accepted if float(trade["net_pnl"]) > 0]
    losers = [trade for trade in accepted if float(trade["net_pnl"]) < 0]
    profit = sum(float(trade["net_pnl"]) for trade in winners)
    loss = abs(sum(float(trade["net_pnl"]) for trade in losers))
    running = 10_000.0
    peak = running
    drawdown = 0.0
    for trade in sorted(accepted, key=lambda item: int(item["exit_time"])):
        running += float(trade["net_pnl"])
        peak = max(peak, running)
        drawdown = max(drawdown, (peak - running) / peak * 100 if peak else 0.0)
    days = max((end_ms - start_ms) / base.DAY_MS, 1 / 24)
    metrics = {
        "trades": len(accepted),
        "trades_per_day": round(len(accepted) / days, 2),
        "win_rate": round(len(winners) / len(accepted) * 100, 2) if accepted else 0.0,
        "profit_factor": round(profit / loss, 3) if loss else None,
        "average_realized_r": round(
            sum(float(trade["realized_r"]) for trade in accepted) / len(accepted), 4
        ) if accepted else 0.0,
        "return_percent": round((running / 10_000.0 - 1) * 100, 2),
        "max_drawdown_percent": round(drawdown, 2),
        "by_source": {
            source: sum(trade["source"] == source for trade in accepted)
            for source in ("baseline", "classifier")
        },
    }
    return metrics, accepted


def main() -> int:
    histories = base.load_histories(
        ROOT / "data/market_cache_orderflow_15m_430d",
        [
            "AAVEUSDT", "ADAUSDT", "APTUSDT", "BTCUSDT", "CRVUSDT",
            "DOGEUSDT", "ETHUSDT", "FILUSDT", "LINKUSDT", "NEARUSDT",
            "RENDERUSDT", "SOLUSDT", "TAOUSDT", "XLMUSDT", "XRPUSDT",
        ],
    )
    btc_by_time = {candle.open_time: candle for candle in histories["BTCUSDT"]}
    features: dict[str, list[list[float] | None]] = {}
    atr_values: dict[str, list[float | None]] = {}
    for index, symbol in enumerate(histories):
        features[symbol], atr_values[symbol] = base.build_base_features(
            histories[symbol], btc_by_time, index, len(histories)
        )
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    audit_start = data_end - 430 * base.DAY_MS
    validation_start = audit_start + 120 * base.DAY_MS
    holdout_start = data_end - 60 * base.DAY_MS
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text())
    baseline_symbols = list(config["market"]["symbols"])
    baseline_histories = {symbol: histories[symbol] for symbol in baseline_symbols}
    baseline_strategies: dict[str, dict[str, Any]] = {}
    baseline_prepared: dict[str, Any] = {}
    baseline_validation: dict[str, dict[str, Any]] = {}
    baseline_validation_stress: dict[str, dict[str, Any]] = {}
    for mode, allow_longs in (("short_only", False), ("both_sides", True)):
        strategy = dict(config["strategy"])
        strategy["allow_longs"] = allow_longs
        prepared = prepare_histories(baseline_histories, strategy)
        baseline_strategies[mode] = strategy
        baseline_prepared[mode] = prepared
        baseline_validation[mode] = run_portfolio_backtest(
            baseline_histories, strategy, config["account"], config["broker"],
            validation_start, holdout_start,
            fee_bps=5.0, maker_fee_bps=2.0, slippage_bps=2.0,
            prepared_data=prepared,
        )
        baseline_validation_stress[mode] = run_portfolio_backtest(
            baseline_histories, strategy, config["account"], config["broker"],
            validation_start, holdout_start,
            fee_bps=5.0, maker_fee_bps=4.0, slippage_bps=3.0,
            prepared_data=prepared,
        )
    models = build_models(histories, features, atr_values, validation_start, holdout_start)
    print("precomputing classifier scores", flush=True)
    scores = base.precompute_scores(
        histories, features, atr_values, models, validation_start, holdout_start
    )
    validation_month_count = math.ceil((holdout_start - validation_start) / (30 * base.DAY_MS))
    candidates: list[dict[str, Any]] = []
    for quantile in POLICIES:
        classifier_metrics, classifier_trades = base.run_portfolio(
            histories,
            features,
            atr_values,
            models,
            scores,
            validation_start,
            holdout_start,
            quantile,
        )
        classifier_stress, classifier_stress_trades = base.run_portfolio(
            histories,
            features,
            atr_values,
            models,
            scores,
            validation_start,
            holdout_start,
            quantile,
            fee_bps=7.0,
            maker_fee_bps=4.0,
            slippage_bps=4.0,
        )
        for mode in baseline_strategies:
            ensemble, ensemble_trades = merge_portfolio(
                baseline_validation[mode]["trade_log"], classifier_trades,
                validation_start, holdout_start,
            )
            ensemble_stress, _ensemble_stress_trades = merge_portfolio(
                baseline_validation_stress[mode]["trade_log"], classifier_stress_trades,
                validation_start, holdout_start,
            )
            candidates.append(
                {
                    "baseline_mode": mode,
                    "signal_quantile": quantile,
                    "classifier": compact(classifier_metrics),
                    "classifier_stress": compact(classifier_stress),
                    "ensemble": ensemble,
                    "ensemble_stress": ensemble_stress,
                    "profitable_months": profitable_months(
                        ensemble_trades, validation_start, validation_month_count
                    ),
                }
            )
            print(json.dumps(candidates[-1], sort_keys=True), flush=True)
    eligible = [
        item for item in candidates
        if 3.5 <= float(item["ensemble"]["trades_per_day"]) <= 6.5
        and float(item["ensemble"]["win_rate"]) >= 45.0
        and float(item["ensemble"]["profit_factor"] or 0) >= 1.1
        and float(item["ensemble_stress"]["profit_factor"] or 0) >= 1.0
        and int(item["profitable_months"]) >= 5
    ]
    selected = max(
        eligible or candidates,
        key=lambda item: (
            -abs(float(item["ensemble"]["trades_per_day"]) - 5.0),
            float(item["ensemble"]["profit_factor"] or 0),
            int(item["profitable_months"]),
        ),
    )
    validation_passed = bool(eligible)
    report: dict[str, Any] = {
        "method": {
            "model": "HistGradientBoostingClassifier",
            "target": "probability of positive net R for next-open 1:2 trade",
            "training": "90d fit, 30d cost-aware calibration, next 30d trade",
            "features": "price, trend, volatility, volume, taker imbalance, BTC regime, calendar, symbol",
            "costs": "taker entry/stop/time exit; maker target without slippage",
            "ensemble": "pullback baseline plus classifier; shared 4-position and 8-trade/day limits",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "baseline_validation": {
            mode: compact(result) for mode, result in baseline_validation.items()
        },
        "baseline_validation_stress": {
            mode: compact(result) for mode, result in baseline_validation_stress.items()
        },
        "validation_candidates": candidates,
        "selected_on_validation": selected,
        "validation_passed": validation_passed,
        "holdout_opened": validation_passed,
    }
    if validation_passed:
        quantile = selected["signal_quantile"]
        baseline_mode = str(selected["baseline_mode"])
        baseline_strategy = baseline_strategies[baseline_mode]
        prepared = baseline_prepared[baseline_mode]
        baseline_holdout = run_portfolio_backtest(
            baseline_histories, baseline_strategy, config["account"], config["broker"],
            holdout_start, data_end,
            fee_bps=5.0, maker_fee_bps=2.0, slippage_bps=2.0,
            prepared_data=prepared,
        )
        baseline_holdout_stress = run_portfolio_backtest(
            baseline_histories, baseline_strategy, config["account"], config["broker"],
            holdout_start, data_end,
            fee_bps=5.0, maker_fee_bps=4.0, slippage_bps=3.0,
            prepared_data=prepared,
        )
        holdout_models = build_models(histories, features, atr_values, holdout_start, data_end)
        holdout_scores = base.precompute_scores(
            histories, features, atr_values, holdout_models, holdout_start, data_end
        )
        holdout_classifier, holdout_classifier_trades = base.run_portfolio(
            histories,
            features,
            atr_values,
            holdout_models,
            holdout_scores,
            holdout_start,
            data_end,
            quantile,
        )
        holdout_classifier_stress, holdout_classifier_stress_trades = base.run_portfolio(
            histories,
            features,
            atr_values,
            holdout_models,
            holdout_scores,
            holdout_start,
            data_end,
            quantile,
            fee_bps=7.0,
            maker_fee_bps=4.0,
            slippage_bps=4.0,
        )
        holdout_ensemble, holdout_ensemble_trades = merge_portfolio(
            baseline_holdout["trade_log"], holdout_classifier_trades,
            holdout_start, data_end,
        )
        holdout_ensemble_stress, _ = merge_portfolio(
            baseline_holdout_stress["trade_log"], holdout_classifier_stress_trades,
            holdout_start, data_end,
        )
        report["final_holdout_once"] = holdout_ensemble
        report["final_holdout_stress"] = holdout_ensemble_stress
        report["holdout_classifier"] = compact(holdout_classifier)
        report["holdout_classifier_stress"] = compact(holdout_classifier_stress)
        report["holdout_months"] = base.monthly_metrics(
            holdout_ensemble_trades, holdout_start, 2
        )
    output = ROOT / "research/orderflow_classifier_walkforward.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "selected": selected,
        "validation_passed": validation_passed,
        "final_holdout_once": report.get("final_holdout_once"),
        "output": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
