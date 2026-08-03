#!/usr/bin/env python3
"""Small no-lookahead grid for a performance-gated monthly symbol universe."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_backtest import prepare_histories, run_portfolio_backtest  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics, load_cached_histories  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.demo.regime-scalp.example.json"))
    parser.add_argument("--cache-dir", default=str(ROOT / "data/market_cache_15m_430d"))
    parser.add_argument("--output", default=str(ROOT / "research/rolling_universe_grid.json"))
    return parser.parse_args()


def select_positive_periods(
    symbols: list[str],
    individual_trades: dict[str, list[dict[str, Any]]],
    start_ms: int,
    months: int,
    selection_days: int,
    universe_size: int,
    minimum_trades: int = 8,
) -> tuple[list[tuple[int, int, set[str]]], list[dict[str, Any]]]:
    periods: list[tuple[int, int, set[str]]] = []
    records: list[dict[str, Any]] = []
    for month in range(months):
        period_start = start_ms + month * 30 * DAY_MS
        period_end = period_start + 30 * DAY_MS
        calibration_start = period_start - selection_days * DAY_MS
        ranked: list[tuple[float, int, str]] = []
        for symbol in symbols:
            calibration = [
                trade
                for trade in individual_trades[symbol]
                if calibration_start <= int(trade["exit_time"]) < period_start
            ]
            count = len(calibration)
            total_r = sum(float(trade["realized_r"]) for trade in calibration)
            score = total_r / (count + 20) if count >= minimum_trades else -999.0
            if score > 0:
                ranked.append((score, count, symbol))
        ranked.sort(reverse=True)
        chosen = [symbol for _, _, symbol in ranked[:universe_size]]
        periods.append((period_start, period_end, set(chosen)))
        records.append(
            {
                "start_utc": dt.datetime.fromtimestamp(
                    period_start / 1000, dt.timezone.utc
                ).isoformat(),
                "symbols": chosen,
                "scores": {
                    symbol: round(score, 6)
                    for score, _, symbol in ranked[:universe_size]
                },
            }
        )
    return periods, records


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


def main() -> int:
    args = parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    strategy = dict(config["strategy"])
    cache_dir = Path(args.cache_dir)
    symbols = sorted(path.name.split("-")[0] for path in cache_dir.glob("*-15m-*.json"))
    histories = load_cached_histories(cache_dir, symbols, str(config["market"]["interval"]))
    prepared = prepare_histories(histories, strategy)

    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    audit_start = data_end - 420 * DAY_MS
    validation_start = audit_start + 90 * DAY_MS
    holdout_start = audit_start + 360 * DAY_MS
    holdout_end = data_end

    account = dict(config["account"])
    calibration_account = dict(account)
    calibration_account.update(
        {"max_open_positions": 1, "max_daily_trades": 100, "max_daily_loss_percent": 100.0}
    )
    broker = dict(config["broker"])
    individual_trades: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        symbol_prepared = (
            {symbol: prepared[0][symbol]},
            set(prepared[0][symbol]["index_by_time"]),
        )
        result = run_portfolio_backtest(
            {symbol: histories[symbol]},
            strategy,
            calibration_account,
            broker,
            audit_start,
            holdout_end,
            fee_bps=5.0,
            maker_fee_bps=2.0,
            slippage_bps=2.0,
            prepared_data=symbol_prepared,
        )
        individual_trades[symbol] = result["trade_log"]

    common = {
        "histories": histories,
        "strategy": strategy,
        "account": account,
        "broker": broker,
        "fee_bps": 5.0,
        "maker_fee_bps": 2.0,
        "slippage_bps": 2.0,
        "prepared_data": prepared,
    }
    candidates: list[dict[str, Any]] = []
    period_cache: dict[tuple[int, int], tuple[list[tuple[int, int, set[str]]], list[dict[str, Any]]]] = {}
    for selection_days in (60, 90, 120):
        for universe_size in (8, 12, 16):
            periods, records = select_positive_periods(
                symbols,
                individual_trades,
                validation_start,
                months=9,
                selection_days=selection_days,
                universe_size=universe_size,
            )
            period_cache[(selection_days, universe_size)] = (periods, records)
            result = run_portfolio_backtest(
                start_ms=validation_start,
                end_ms=holdout_start,
                active_universe_periods=periods,
                **common,
            )
            months = [basic_metrics(result["trade_log"], start, end) for start, end, _ in periods]
            candidates.append(
                {
                    "selection_days": selection_days,
                    "universe_size": universe_size,
                    "validation": compact(result),
                    "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
                    "months": months,
                }
            )

    eligible = [
        item
        for item in candidates
        if 3.5 <= float(item["validation"]["trades_per_day"] or 0) <= 6.5
        and float(item["validation"]["profit_factor"] or 0) > 1.0
        and int(item["profitable_months"]) >= 5
    ]
    ranked = eligible or candidates
    selected = max(
        ranked,
        key=lambda item: (
            float(item["validation"]["profit_factor"] or 0),
            int(item["profitable_months"]),
            -float(item["validation"]["max_drawdown_percent"] or 999),
        ),
    )
    selected_key = (int(selected["selection_days"]), int(selected["universe_size"]))
    holdout_periods, holdout_selection = select_positive_periods(
        symbols,
        individual_trades,
        holdout_start,
        months=2,
        selection_days=selected_key[0],
        universe_size=selected_key[1],
    )
    holdout = run_portfolio_backtest(
        start_ms=holdout_start,
        end_ms=holdout_end,
        active_universe_periods=holdout_periods,
        **common,
    )
    validation_periods = period_cache[selected_key][0]
    validation_stress = run_portfolio_backtest(
        histories=histories,
        strategy=strategy,
        account=account,
        broker=broker,
        start_ms=validation_start,
        end_ms=holdout_start,
        fee_bps=5.0,
        maker_fee_bps=4.0,
        slippage_bps=3.0,
        prepared_data=prepared,
        active_universe_periods=validation_periods,
    )
    report = {
        "method": {
            "description": "Monthly positive-expectancy symbol gate using only earlier trades",
            "grid": {"selection_days": [60, 90, 120], "universe_size": [8, 12, 16]},
            "minimum_calibration_trades": 8,
            "score": "sum(realized_r) / (trade_count + 20), require score > 0",
            "holdout_used_in_selection": False,
        },
        "candidates": candidates,
        "selected": selected,
        "selected_validation_stress_maker4_slippage3": compact(validation_stress),
        "final_holdout_once": compact(holdout),
        "holdout_months": [
            basic_metrics(holdout["trade_log"], start, end) for start, end, _ in holdout_periods
        ],
        "holdout_selection": holdout_selection,
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "selected": selected,
                "validation_stress": compact(validation_stress),
                "final_holdout_once": compact(holdout),
                "output": str(output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
