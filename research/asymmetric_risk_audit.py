#!/usr/bin/env python3
"""Validation and confirmation audit for the asymmetric 15m profile."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import load_config  # noqa: E402
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


def monthly(trades: list[dict[str, Any]], start: int, months: int) -> list[dict[str, Any]]:
    return [
        basic_metrics(trades, start + month * 30 * DAY_MS, start + (month + 1) * 30 * DAY_MS)
        for month in range(months)
    ]


def run_period(
    histories: dict[str, list[Any]],
    strategy: dict[str, Any],
    account: dict[str, Any],
    broker: dict[str, Any],
    prepared: Any,
    start: int,
    end: int,
    *,
    stress: bool,
) -> dict[str, Any]:
    return run_portfolio_backtest(
        histories=histories,
        strategy=strategy,
        account=account,
        broker=broker,
        start_ms=start,
        end_ms=end,
        fee_bps=5.0,
        maker_fee_bps=4.0 if stress else 2.0,
        slippage_bps=3.0 if stress else 2.0,
        prepared_data=prepared,
    )


def main() -> int:
    config = load_config(ROOT / "config.paper.asymmetric-15m.example.json")
    strategy = dict(config["strategy"])
    symbols = list(config["market"]["symbols"])
    histories = load_cached_histories(ROOT / "data/market_cache_15m_430d", symbols, "15m")
    prepared = prepare_histories(histories, strategy)
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    confirmation_start = data_end - 60 * DAY_MS
    validation_start = confirmation_start - 270 * DAY_MS
    broker = dict(config["broker"])

    candidates: list[dict[str, Any]] = []
    for long_risk in (0.025, 0.05, 0.075, 0.10, 0.15):
        account = dict(config["account"])
        account["long_risk_per_trade_percent"] = long_risk
        base = run_period(
            histories, strategy, account, broker, prepared,
            validation_start, confirmation_start, stress=False,
        )
        stress = run_period(
            histories, strategy, account, broker, prepared,
            validation_start, confirmation_start, stress=True,
        )
        months = monthly(base["trade_log"], validation_start, 9)
        candidates.append(
            {
                "long_risk_percent": long_risk,
                "short_risk_percent": account["short_risk_per_trade_percent"],
                "validation": compact(base),
                "validation_stress": compact(stress),
                "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
                "months": months,
            }
        )

    eligible = [
        item for item in candidates
        if 4.0 <= float(item["validation"]["trades_per_day"]) <= 6.0
        and float(item["validation"]["win_rate"]) >= 45.0
        and float(item["validation"]["profit_factor"] or 0) >= 1.10
        and float(item["validation_stress"]["profit_factor"] or 0) >= 1.05
        and int(item["profitable_months"]) >= 6
    ]
    selected = max(
        eligible or candidates,
        key=lambda item: (
            min(
                float(item["validation"]["profit_factor"] or 0),
                float(item["validation_stress"]["profit_factor"] or 0),
            ),
            -abs(float(item["validation"]["trades_per_day"]) - 5.0),
        ),
    )
    account = dict(config["account"])
    account["long_risk_per_trade_percent"] = selected["long_risk_percent"]
    confirmation = run_period(
        histories, strategy, account, broker, prepared,
        confirmation_start, data_end, stress=False,
    )
    confirmation_stress = run_period(
        histories, strategy, account, broker, prepared,
        confirmation_start, data_end, stress=True,
    )
    report = {
        "method": {
            "description": "270d validation risk grid plus 60d confirmation holdout",
            "holdout_note": "confirmation, not sealed; it was inspected during exploratory research",
            "strategy": "15m regime pullback, limit retrace entry, both sides",
            "nominal_reward_risk": round(strategy["target_atr"] / strategy["stop_atr"], 2),
            "costs_base": "maker entry 2bps, maker target 2bps, taker stop/time exit 5bps, 2bps slippage",
            "costs_stress": "maker entry/target 4bps, taker stop/time exit 5bps, 3bps slippage",
            "selection_gate": "4-6 trades/day, WR >= 45%, PF >= 1.10, stress PF >= 1.05, 6/9 profitable months",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "validation_candidates": candidates,
        "selected_on_validation": selected,
        "validation_passed": selected in eligible,
        "confirmation_60d": compact(confirmation),
        "confirmation_stress_60d": compact(confirmation_stress),
        "confirmation_months": monthly(confirmation["trade_log"], confirmation_start, 2),
        "rejected_exact_rr_2_note": (
            "SL 1.8 ATR / TP 3.6 ATR was not selected: exploratory validation WR was below 45%; "
            "the selected 1:1.56 profile had stronger base and stressed PF."
        ),
    }
    output = ROOT / "research/asymmetric_risk_audit.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "selected": selected,
        "confirmation_60d": report["confirmation_60d"],
        "confirmation_stress_60d": report["confirmation_stress_60d"],
        "output": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
