#!/usr/bin/env python3
"""Three-way, fee-aware audit of a range mean-reversion companion strategy."""

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
    return {
        key: result.get(key)
        for key in (
            "trades",
            "trades_per_day",
            "win_rate",
            "profit_factor",
            "average_realized_r",
            "return_percent",
            "max_drawdown_percent",
        )
    }


def monthly(trades: list[dict[str, Any]], start: int, count: int) -> list[dict[str, Any]]:
    return [
        basic_metrics(trades, start + month * 30 * DAY_MS, start + (month + 1) * 30 * DAY_MS)
        for month in range(count)
    ]


def strategy_from(base: dict[str, Any], values: tuple[Any, ...]) -> dict[str, Any]:
    band_length, band_stddev, max_adx, max_slope, rsi_pair, max_distance = values
    strategy = dict(base)
    strategy.update(
        {
            "type": "intraday_mean_reversion",
            "entry_order_type": "market",
            "target_order_type": "limit",
            "band_length": band_length,
            "band_stddev": band_stddev,
            "max_mean_reversion_adx": max_adx,
            "max_mean_reversion_slope_percent": max_slope,
            "mean_reversion_long_rsi_max": rsi_pair[0],
            "mean_reversion_short_rsi_min": rsi_pair[1],
            "max_distance_from_regime_atr": max_distance,
            "min_confirmation_body_ratio": 0.15,
            "stop_atr": 1.2,
            "target_atr": 1.8,
            "max_holding_bars": 16,
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
            (20, 32),
            (1.8, 2.2),
            (18.0, 24.0),
            (0.04, 0.08),
            ((38.0, 62.0), (42.0, 58.0)),
            (1.25, 2.0),
        )
    )
    prepared_by_band: dict[int, tuple[dict[str, dict[str, Any]], set[int]]] = {}
    candidates: list[dict[str, Any]] = []
    for values in grid:
        strategy = strategy_from(config["strategy"], values)
        band_length = int(strategy["band_length"])
        if band_length not in prepared_by_band:
            prepared_by_band[band_length] = prepare_histories(histories, strategy)
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
            prepared_data=prepared_by_band[band_length],
        )
        months = monthly(result["trade_log"], development_start, 8)
        avg_r = float(result["average_realized_r"])
        count = int(result["trades"])
        conservative_score = avg_r - 1.0 / math.sqrt(max(count, 1))
        candidates.append(
            {
                "parameters": {
                    "band_length": values[0],
                    "band_stddev": values[1],
                    "max_adx": values[2],
                    "max_slope_percent": values[3],
                    "long_rsi_max": values[4][0],
                    "short_rsi_min": values[4][1],
                    "max_distance_atr": values[5],
                },
                "development": compact(result),
                "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
                "conservative_score": round(conservative_score, 6),
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
    selected_values = (
        p["band_length"],
        p["band_stddev"],
        p["max_adx"],
        p["max_slope_percent"],
        (p["long_rsi_max"], p["short_rsi_min"]),
        p["max_distance_atr"],
    )
    strategy = strategy_from(config["strategy"], selected_values)
    prepared = prepared_by_band[int(strategy["band_length"])]
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

    output = ROOT / "research/mean_reversion_split.json"
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
