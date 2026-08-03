#!/usr/bin/env python3
"""Three-way, fee-aware audit of a trend breakout companion strategy."""

from __future__ import annotations

import datetime as dt
import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_backtest import prepare_histories, run_portfolio_backtest  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics, load_cached_histories  # noqa: E402


def compact(result: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "trades",
        "trades_per_day",
        "win_rate",
        "profit_factor",
        "average_realized_r",
        "return_percent",
        "max_drawdown_percent",
    )
    return {key: result.get(key) for key in keys}


def monthly(trades: list[dict[str, Any]], start: int, count: int) -> list[dict[str, Any]]:
    return [
        basic_metrics(trades, start + month * 30 * DAY_MS, start + (month + 1) * 30 * DAY_MS)
        for month in range(count)
    ]


def make_strategy(base: dict[str, Any], values: tuple[Any, ...]) -> dict[str, Any]:
    lookback, max_extension, min_adx, volume_factor, body_ratio = values
    strategy = dict(base)
    strategy.update(
        {
            "type": "intraday_breakout",
            "entry_order_type": "market",
            "target_order_type": "limit",
            "breakout_lookback": lookback,
            "max_breakout_extension_atr": max_extension,
            "min_adx": min_adx,
            "min_volume_factor": volume_factor,
            "min_confirmation_body_ratio": body_ratio,
            "long_rsi_min": 50.0,
            "long_rsi_max": 82.0,
            "short_rsi_min": 18.0,
            "short_rsi_max": 50.0,
            "stop_atr": 1.5,
            "target_atr": 2.4,
            "max_holding_bars": 24,
            "cooldown_bars": 4,
            "allow_longs": True,
            "allow_shorts": True,
        }
    )
    return strategy


def main() -> int:
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text(encoding="utf-8"))
    symbols = list(config["market"]["symbols"])
    histories = load_cached_histories(ROOT / "data/market_cache_15m_430d", symbols, "15m")
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    development_start = data_end - 420 * DAY_MS
    validation_start = data_end - 180 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS
    account = dict(config["account"])
    broker = dict(config["broker"])

    grid = list(
        itertools.product(
            (12, 24, 48),
            (0.3, 0.7),
            (15.0, 22.0),
            (0.8, 1.2),
            (0.3, 0.5),
        )
    )
    candidates: list[dict[str, Any]] = []
    prepared_by_lookback: dict[int, tuple[dict[str, dict[str, Any]], set[int]]] = {}
    for values in grid:
        strategy = make_strategy(config["strategy"], values)
        lookback = int(strategy["breakout_lookback"])
        if lookback not in prepared_by_lookback:
            prepared_by_lookback[lookback] = prepare_histories(histories, strategy)
        result = run_portfolio_backtest(
            histories,
            strategy,
            account,
            broker,
            development_start,
            validation_start,
            fee_bps=5.0,
            maker_fee_bps=2.0,
            slippage_bps=2.0,
            prepared_data=prepared_by_lookback[lookback],
        )
        months = monthly(result["trade_log"], development_start, 8)
        count = int(result["trades"])
        score = float(result["average_realized_r"]) - 1.0 / math.sqrt(max(count, 1))
        candidates.append(
            {
                "parameters": {
                    "lookback": values[0],
                    "max_extension_atr": values[1],
                    "min_adx": values[2],
                    "min_volume_factor": values[3],
                    "min_body_ratio": values[4],
                },
                "development": compact(result),
                "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
                "conservative_score": round(score, 6),
            }
        )

    eligible = [
        item
        for item in candidates
        if 1.0 <= float(item["development"]["trades_per_day"] or 0) <= 4.0
        and float(item["development"]["profit_factor"] or 0) >= 1.1
        and int(item["profitable_months"]) >= 5
    ]
    selected = max(eligible or candidates, key=lambda item: float(item["conservative_score"]))
    p = selected["parameters"]
    strategy = make_strategy(
        config["strategy"],
        (p["lookback"], p["max_extension_atr"], p["min_adx"], p["min_volume_factor"], p["min_body_ratio"]),
    )
    prepared = prepared_by_lookback[int(strategy["breakout_lookback"])]
    common = {
        "histories": histories,
        "strategy": strategy,
        "account": account,
        "broker": broker,
        "prepared_data": prepared,
    }
    validation = run_portfolio_backtest(
        start_ms=validation_start,
        end_ms=holdout_start,
        fee_bps=5.0,
        maker_fee_bps=2.0,
        slippage_bps=2.0,
        **common,
    )
    validation_stress = run_portfolio_backtest(
        start_ms=validation_start,
        end_ms=holdout_start,
        fee_bps=7.0,
        maker_fee_bps=4.0,
        slippage_bps=4.0,
        **common,
    )
    validation_months = monthly(validation["trade_log"], validation_start, 4)
    validation_passed = (
        1.0 <= float(validation["trades_per_day"]) <= 4.0
        and float(validation["profit_factor"] or 0) >= 1.1
        and float(validation_stress["profit_factor"] or 0) >= 1.0
        and sum(float(item["net_pnl"]) > 0 for item in validation_months) >= 3
    )
    report: dict[str, Any] = {
        "method": {
            "description": "240d development, 120d validation, sealed 60d holdout",
            "candidate_count": len(candidates),
            "symbols": symbols,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "selected_on_development": selected,
        "strategy": strategy,
        "validation_120d": compact(validation),
        "validation_stress_taker7_maker4_slippage4": compact(validation_stress),
        "validation_months": validation_months,
        "validation_passed": validation_passed,
        "holdout_opened": validation_passed,
        "development_candidates": sorted(
            candidates, key=lambda item: float(item["conservative_score"]), reverse=True
        ),
    }
    if validation_passed:
        holdout = run_portfolio_backtest(
            start_ms=holdout_start,
            end_ms=data_end,
            fee_bps=5.0,
            maker_fee_bps=2.0,
            slippage_bps=2.0,
            **common,
        )
        holdout_stress = run_portfolio_backtest(
            start_ms=holdout_start,
            end_ms=data_end,
            fee_bps=7.0,
            maker_fee_bps=4.0,
            slippage_bps=4.0,
            **common,
        )
        report["final_holdout_once"] = compact(holdout)
        report["final_holdout_stress"] = compact(holdout_stress)
        report["holdout_months"] = monthly(holdout["trade_log"], holdout_start, 2)

    output = ROOT / "research/breakout_split.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "selected": selected,
                "validation": compact(validation),
                "validation_stress": compact(validation_stress),
                "validation_passed": validation_passed,
                "final_holdout_once": report.get("final_holdout_once"),
                "output": str(output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
