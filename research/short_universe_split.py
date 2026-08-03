#!/usr/bin/env python3
"""Three-way split audit for a broader short-only symbol universe."""

from __future__ import annotations

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


def month_metrics(trades: list[dict[str, Any]], start: int, months: int) -> list[dict[str, Any]]:
    return [
        basic_metrics(
            trades,
            start + month * 30 * DAY_MS,
            start + (month + 1) * 30 * DAY_MS,
        )
        for month in range(months)
    ]


def main() -> int:
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text(encoding="utf-8"))
    strategy = dict(config["strategy"])
    strategy["allow_longs"] = False
    cache_dir = ROOT / "data/market_cache_15m_430d"
    symbols = sorted(path.name.split("-")[0] for path in cache_dir.glob("*-15m-*.json"))
    histories = load_cached_histories(cache_dir, symbols, "15m")
    prepared = prepare_histories(histories, strategy)

    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    development_start = data_end - 420 * DAY_MS
    validation_start = data_end - 180 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS

    account = dict(config["account"])
    calibration_account = dict(account)
    calibration_account.update(
        {"max_open_positions": 1, "max_daily_trades": 100, "max_daily_loss_percent": 100.0}
    )
    broker = dict(config["broker"])
    ranking: list[tuple[float, str, dict[str, Any]]] = []
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
            development_start,
            validation_start,
            fee_bps=5.0,
            maker_fee_bps=2.0,
            slippage_bps=2.0,
            prepared_data=symbol_prepared,
        )
        count = int(result["trades"])
        total_r = sum(float(trade["realized_r"]) for trade in result["trade_log"])
        score = total_r / (count + 30) if count >= 30 else -999.0
        ranking.append((score, symbol, compact(result)))
    ranking.sort(reverse=True)

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
    for size in (8, 12, 16, 20, 24):
        selected_symbols = [symbol for score, symbol, _ in ranking if score > 0][:size]
        active = [(development_start, validation_start, set(selected_symbols))]
        result = run_portfolio_backtest(
            start_ms=development_start,
            end_ms=validation_start,
            active_universe_periods=active,
            **common,
        )
        months = month_metrics(result["trade_log"], development_start, 8)
        candidates.append(
            {
                "requested_size": size,
                "symbols": selected_symbols,
                "development": compact(result),
                "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
                "months": months,
            }
        )

    eligible = [
        item
        for item in candidates
        if 3.5 <= float(item["development"]["trades_per_day"] or 0) <= 6.0
        and float(item["development"]["profit_factor"] or 0) >= 1.1
        and int(item["profitable_months"]) >= 5
    ]
    ranked_candidates = eligible or candidates
    selected = max(
        ranked_candidates,
        key=lambda item: (
            float(item["development"]["profit_factor"] or 0),
            int(item["profitable_months"]),
        ),
    )
    chosen = set(selected["symbols"])
    validation_active = [(validation_start, holdout_start, chosen)]
    validation = run_portfolio_backtest(
        start_ms=validation_start,
        end_ms=holdout_start,
        active_universe_periods=validation_active,
        **common,
    )
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
        active_universe_periods=validation_active,
    )
    validation_months = month_metrics(validation["trade_log"], validation_start, 4)
    validation_passed = (
        3.5 <= float(validation["trades_per_day"]) <= 6.0
        and float(validation["profit_factor"] or 0) >= 1.1
        and float(validation_stress["profit_factor"] or 0) >= 1.0
        and sum(float(item["net_pnl"]) > 0 for item in validation_months) >= 3
    )

    report: dict[str, Any] = {
        "method": {
            "description": "240d development, 120d validation, 60d sealed holdout",
            "side": "short only",
            "symbol_score": "sum(realized_r) / (trade_count + 30), require score > 0",
            "universe_sizes_checked_on_development": [8, 12, 16, 20, 24],
            "validation_gate": "3.5-6 trades/day, PF >= 1.10, stress PF >= 1.00, 3/4 profitable months",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "symbol_ranking": [
            {"symbol": symbol, "score": round(score, 6), "development": metrics}
            for score, symbol, metrics in ranking
        ],
        "development_candidates": candidates,
        "selected_on_development": selected,
        "validation_120d": compact(validation),
        "validation_stress_maker4_slippage3": compact(validation_stress),
        "validation_months": validation_months,
        "validation_passed": validation_passed,
        "holdout_opened": validation_passed,
    }
    if validation_passed:
        holdout_active = [(holdout_start, data_end, chosen)]
        holdout = run_portfolio_backtest(
            start_ms=holdout_start,
            end_ms=data_end,
            active_universe_periods=holdout_active,
            **common,
        )
        holdout_stress = run_portfolio_backtest(
            histories=histories,
            strategy=strategy,
            account=account,
            broker=broker,
            start_ms=holdout_start,
            end_ms=data_end,
            fee_bps=5.0,
            maker_fee_bps=4.0,
            slippage_bps=3.0,
            prepared_data=prepared,
            active_universe_periods=holdout_active,
        )
        report["final_holdout_once"] = compact(holdout)
        report["final_holdout_stress_maker4_slippage3"] = compact(holdout_stress)
        report["holdout_months"] = month_metrics(holdout["trade_log"], holdout_start, 2)

    output = ROOT / "research/short_universe_split.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "selected_symbols": sorted(chosen),
                "development": selected["development"],
                "validation": compact(validation),
                "validation_stress": compact(validation_stress),
                "validation_months": validation_months,
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
