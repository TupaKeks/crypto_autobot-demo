#!/usr/bin/env python3
"""Three-way audit of session-filtered long trades plus the frozen short baseline."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import load_config  # noqa: E402
from portfolio_backtest import prepare_histories, run_portfolio_backtest  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics, load_cached_histories  # noqa: E402


SESSIONS = {
    "all": set(range(24)),
    "asia_00_08": set(range(0, 8)),
    "europe_08_16": set(range(8, 16)),
    "us_16_24": set(range(16, 24)),
    "europe_us_08_24": set(range(8, 24)),
}
SIZES = (8, 12, 16, 20, 24)


def compact(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "trades", "trades_per_day", "win_rate", "profit_factor",
            "average_realized_r", "return_percent", "max_drawdown_percent",
        )
    }


def monthly(trades: list[dict[str, Any]], start: int, count: int) -> list[dict[str, Any]]:
    return [
        basic_metrics(trades, start + month * 30 * DAY_MS, start + (month + 1) * 30 * DAY_MS)
        for month in range(count)
    ]


def utc_hour(timestamp: int) -> int:
    return dt.datetime.fromtimestamp(timestamp / 1000, dt.timezone.utc).hour


def main() -> int:
    config = load_config(ROOT / "config.paper.asymmetric-15m.example.json")
    strategy = dict(config["strategy"])
    strategy.update({"allow_longs": True, "allow_shorts": True})
    cache_dir = ROOT / "data/market_cache_15m_430d"
    symbols = sorted({path.name.split("-")[0] for path in cache_dir.glob("*-15m-*.json")})
    histories = load_cached_histories(cache_dir, symbols, "15m")
    prepared = prepare_histories(histories, strategy)

    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    development_start = data_end - 430 * DAY_MS
    validation_start = development_start + 120 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS
    short_symbols = set(config["market"]["symbols"])
    account = dict(config["account"])
    account.update(
        {
            "long_risk_per_trade_percent": 0.15,
            "short_risk_per_trade_percent": 0.15,
        }
    )
    calibration_account = dict(account)
    calibration_account.update(
        {"max_open_positions": 1, "max_daily_trades": 100, "max_daily_loss_percent": 100.0}
    )
    broker = dict(config["broker"])

    def long_only(_symbol: str, side: str, _timestamp: int) -> bool:
        return side == "long"

    individual_long_trades: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        symbol_prepared = (
            {symbol: prepared[0][symbol]},
            set(prepared[0][symbol]["index_by_time"]),
        )
        result = run_portfolio_backtest(
            histories={symbol: histories[symbol]},
            strategy=strategy,
            account=calibration_account,
            broker=broker,
            start_ms=development_start,
            end_ms=validation_start,
            fee_bps=5.0,
            maker_fee_bps=2.0,
            slippage_bps=2.0,
            prepared_data=symbol_prepared,
            signal_filter=long_only,
        )
        individual_long_trades[symbol] = result["trade_log"]

    common = {
        "histories": histories,
        "strategy": strategy,
        "account": account,
        "broker": broker,
        "prepared_data": prepared,
    }
    candidates: list[dict[str, Any]] = []
    rankings: dict[str, list[dict[str, Any]]] = {}
    for session_name, hours in SESSIONS.items():
        ranking: list[tuple[float, str, dict[str, Any]]] = []
        for symbol, trades in individual_long_trades.items():
            selected = [
                trade for trade in trades
                if utc_hour(int(trade["entry_time"])) in hours
            ]
            count = len(selected)
            total_r = sum(float(trade["realized_r"]) for trade in selected)
            score = total_r / (count + 20) if count >= 10 else -999.0
            winners = sum(float(trade["net_pnl"]) > 0 for trade in selected)
            ranking.append(
                (
                    score,
                    symbol,
                    {
                        "trades": count,
                        "win_rate": round(winners / count * 100, 2) if count else 0.0,
                        "total_r": round(total_r, 4),
                    },
                )
            )
        ranking.sort(reverse=True)
        rankings[session_name] = [
            {"symbol": symbol, "score": round(score, 6), **metrics}
            for score, symbol, metrics in ranking
        ]

        for size in SIZES:
            long_symbols = {symbol for score, symbol, _ in ranking[:size] if score > -999.0}

            def candidate_filter(symbol: str, side: str, timestamp: int) -> bool:
                if side == "short":
                    return symbol in short_symbols
                return symbol in long_symbols and utc_hour(timestamp) in hours

            base = run_portfolio_backtest(
                start_ms=development_start,
                end_ms=validation_start,
                fee_bps=5.0,
                maker_fee_bps=2.0,
                slippage_bps=2.0,
                signal_filter=candidate_filter,
                **common,
            )
            stress = run_portfolio_backtest(
                start_ms=development_start,
                end_ms=validation_start,
                fee_bps=5.0,
                maker_fee_bps=4.0,
                slippage_bps=3.0,
                signal_filter=candidate_filter,
                **common,
            )
            months = monthly(base["trade_log"], development_start, 4)
            candidates.append(
                {
                    "session": session_name,
                    "hours_utc": sorted(hours),
                    "requested_long_symbols": size,
                    "long_symbols": sorted(long_symbols),
                    "development": compact(base),
                    "development_stress": compact(stress),
                    "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
                }
            )
            print(json.dumps(candidates[-1], sort_keys=True), flush=True)

    eligible = [
        item for item in candidates
        if 4.0 <= float(item["development"]["trades_per_day"]) <= 6.0
        and float(item["development"]["win_rate"]) >= 45.0
        and float(item["development"]["profit_factor"] or 0) >= 1.10
        and float(item["development_stress"]["profit_factor"] or 0) >= 1.02
        and int(item["profitable_months"]) >= 3
    ]
    selected = max(
        eligible or candidates,
        key=lambda item: (
            min(
                float(item["development"]["profit_factor"] or 0),
                float(item["development_stress"]["profit_factor"] or 0),
            ),
            -abs(float(item["development"]["trades_per_day"]) - 5.0),
        ),
    )
    chosen_longs = set(selected["long_symbols"])
    chosen_hours = set(selected["hours_utc"])

    def selected_filter(symbol: str, side: str, timestamp: int) -> bool:
        if side == "short":
            return symbol in short_symbols
        return symbol in chosen_longs and utc_hour(timestamp) in chosen_hours

    validation = run_portfolio_backtest(
        start_ms=validation_start,
        end_ms=holdout_start,
        fee_bps=5.0,
        maker_fee_bps=2.0,
        slippage_bps=2.0,
        signal_filter=selected_filter,
        **common,
    )
    validation_stress = run_portfolio_backtest(
        start_ms=validation_start,
        end_ms=holdout_start,
        fee_bps=5.0,
        maker_fee_bps=4.0,
        slippage_bps=3.0,
        signal_filter=selected_filter,
        **common,
    )
    validation_months = monthly(validation["trade_log"], validation_start, 8)
    validation_passed = (
        bool(eligible)
        and 4.0 <= float(validation["trades_per_day"]) <= 6.0
        and float(validation["win_rate"]) >= 45.0
        and float(validation["profit_factor"] or 0) >= 1.10
        and float(validation_stress["profit_factor"] or 0) >= 1.05
        and sum(float(item["net_pnl"]) > 0 for item in validation_months) >= 6
    )
    report: dict[str, Any] = {
        "method": {
            "description": "120d development, 250d validation, sealed 60d holdout",
            "short_component": "frozen 10-symbol baseline",
            "long_component": "broad UTC session plus development-ranked symbols",
            "risk": "0.15% on both sides",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "rankings": rankings,
        "development_candidates": candidates,
        "selected_on_development": selected,
        "development_gate_passed": bool(eligible),
        "validation": compact(validation),
        "validation_stress": compact(validation_stress),
        "validation_months": validation_months,
        "validation_passed": validation_passed,
        "holdout_opened": validation_passed,
    }
    if validation_passed:
        holdout = run_portfolio_backtest(
            start_ms=holdout_start,
            end_ms=data_end,
            fee_bps=5.0,
            maker_fee_bps=2.0,
            slippage_bps=2.0,
            signal_filter=selected_filter,
            **common,
        )
        holdout_stress = run_portfolio_backtest(
            start_ms=holdout_start,
            end_ms=data_end,
            fee_bps=5.0,
            maker_fee_bps=4.0,
            slippage_bps=3.0,
            signal_filter=selected_filter,
            **common,
        )
        report["final_holdout_once"] = compact(holdout)
        report["final_holdout_stress"] = compact(holdout_stress)
        report["holdout_months"] = monthly(holdout["trade_log"], holdout_start, 2)

    output = ROOT / "research/long_session_universe_walkforward.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "selected": selected,
        "validation": report["validation"],
        "validation_stress": report["validation_stress"],
        "validation_passed": validation_passed,
        "final_holdout_once": report.get("final_holdout_once"),
        "output": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
