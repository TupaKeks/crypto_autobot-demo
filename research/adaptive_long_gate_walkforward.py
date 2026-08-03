#!/usr/bin/env python3
"""Walk-forward audit of a monthly quality gate for the pullback long branch."""

from __future__ import annotations

import datetime as dt
import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio_backtest import prepare_histories, run_portfolio_backtest  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics, load_cached_histories  # noqa: E402


def compact(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "trades", "trades_per_day", "win_rate", "profit_factor",
            "average_realized_r", "return_percent", "max_drawdown_percent",
        )
    }


def monthly_metrics(trades: list[dict[str, Any]], start: int, end: int) -> list[dict[str, Any]]:
    count = math.ceil((end - start) / (30 * DAY_MS))
    return [
        basic_metrics(
            trades,
            start + month * 30 * DAY_MS,
            min(start + (month + 1) * 30 * DAY_MS, end),
        )
        for month in range(count)
    ]


def build_gate(
    base_long_trades: list[dict[str, Any]],
    stress_long_trades: list[dict[str, Any]],
    start: int,
    end: int,
    *,
    lookback_days: int,
    minimum_trades: int,
    minimum_win_rate: float,
    minimum_profit_factor: float,
) -> tuple[Callable[[str, str, int], bool], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    enabled_periods: list[tuple[int, int, bool]] = []
    period_count = math.ceil((end - start) / (30 * DAY_MS))
    for period in range(period_count):
        period_start = start + period * 30 * DAY_MS
        period_end = min(period_start + 30 * DAY_MS, end)
        calibration_start = period_start - lookback_days * DAY_MS
        base = basic_metrics(base_long_trades, calibration_start, period_start)
        stress = basic_metrics(stress_long_trades, calibration_start, period_start)
        enabled = (
            int(base["trades"]) >= minimum_trades
            and float(base["win_rate"]) >= minimum_win_rate
            and float(base["average_realized_r"]) > 0
            and float(base["profit_factor"] or 0) >= minimum_profit_factor
            and float(stress["profit_factor"] or 0) >= 1.0
        )
        enabled_periods.append((period_start, period_end, enabled))
        records.append(
            {
                "start_utc": dt.datetime.fromtimestamp(period_start / 1000, dt.timezone.utc).isoformat(),
                "end_utc": dt.datetime.fromtimestamp(period_end / 1000, dt.timezone.utc).isoformat(),
                "longs_enabled": enabled,
                "calibration": base,
                "calibration_stress": stress,
            }
        )

    def signal_filter(_symbol: str, side: str, timestamp: int) -> bool:
        if side == "short":
            return True
        return any(
            period_start <= timestamp < period_end and enabled
            for period_start, period_end, enabled in enabled_periods
        )

    return signal_filter, records


def run_period(
    histories: dict[str, list[Any]],
    strategy: dict[str, Any],
    account: dict[str, Any],
    broker: dict[str, Any],
    prepared: Any,
    long_trades: list[dict[str, Any]],
    long_stress_trades: list[dict[str, Any]],
    start: int,
    end: int,
    policy: dict[str, Any],
) -> dict[str, Any]:
    signal_filter, gate_records = build_gate(
        long_trades,
        long_stress_trades,
        start,
        end,
        **policy,
    )
    common = {
        "histories": histories,
        "strategy": strategy,
        "account": account,
        "broker": broker,
        "start_ms": start,
        "end_ms": end,
        "prepared_data": prepared,
        "signal_filter": signal_filter,
    }
    base = run_portfolio_backtest(
        fee_bps=5.0, maker_fee_bps=2.0, slippage_bps=2.0, **common
    )
    stress = run_portfolio_backtest(
        fee_bps=5.0, maker_fee_bps=4.0, slippage_bps=3.0, **common
    )
    months = monthly_metrics(base["trade_log"], start, end)
    return {
        "base": base,
        "stress": stress,
        "gate_records": gate_records,
        "months": months,
        "profitable_months": sum(float(month["net_pnl"]) > 0 for month in months),
    }


def main() -> int:
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text())
    strategy = dict(config["strategy"])
    strategy["allow_longs"] = True
    symbols = list(config["market"]["symbols"])
    histories = load_cached_histories(ROOT / "data/market_cache_15m_430d", symbols, "15m")
    prepared = prepare_histories(histories, strategy)
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    audit_start = data_end - 430 * DAY_MS
    development_start = audit_start + 90 * DAY_MS
    development_end = development_start + 120 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS
    validation_start = development_end

    def long_only(_symbol: str, side: str, _timestamp: int) -> bool:
        return side == "long"

    long_common = {
        "histories": histories,
        "strategy": strategy,
        "account": config["account"],
        "broker": config["broker"],
        "start_ms": audit_start,
        "end_ms": data_end,
        "prepared_data": prepared,
        "signal_filter": long_only,
    }
    long_base = run_portfolio_backtest(
        fee_bps=5.0, maker_fee_bps=2.0, slippage_bps=2.0, **long_common
    )
    long_stress = run_portfolio_backtest(
        fee_bps=5.0, maker_fee_bps=4.0, slippage_bps=3.0, **long_common
    )

    candidates: list[dict[str, Any]] = []
    for lookback_days, minimum_trades, minimum_win_rate, minimum_profit_factor in itertools.product(
        (30, 60, 90), (20, 35), (42.0, 45.0), (1.0, 1.1)
    ):
        policy = {
            "lookback_days": lookback_days,
            "minimum_trades": minimum_trades,
            "minimum_win_rate": minimum_win_rate,
            "minimum_profit_factor": minimum_profit_factor,
        }
        result = run_period(
            histories, strategy, config["account"], config["broker"], prepared,
            long_base["trade_log"], long_stress["trade_log"],
            development_start, development_end, policy,
        )
        candidates.append(
            {
                "policy": policy,
                "development": compact(result["base"]),
                "development_stress": compact(result["stress"]),
                "profitable_months": result["profitable_months"],
                "enabled_months": sum(item["longs_enabled"] for item in result["gate_records"]),
            }
        )
        print(json.dumps(candidates[-1], sort_keys=True), flush=True)

    eligible = [
        candidate for candidate in candidates
        if 3.5 <= float(candidate["development"]["trades_per_day"]) <= 6.0
        and float(candidate["development"]["win_rate"]) >= 45.0
        and float(candidate["development"]["profit_factor"] or 0) >= 1.1
        and float(candidate["development_stress"]["profit_factor"] or 0) >= 1.0
        and int(candidate["profitable_months"]) >= 3
    ]
    selected = max(
        eligible or candidates,
        key=lambda candidate: (
            int(candidate["profitable_months"]),
            min(
                float(candidate["development"]["profit_factor"] or 0),
                float(candidate["development_stress"]["profit_factor"] or 0),
            ),
            -abs(float(candidate["development"]["trades_per_day"]) - 5.0),
        ),
    )
    validation = run_period(
        histories, strategy, config["account"], config["broker"], prepared,
        long_base["trade_log"], long_stress["trade_log"],
        validation_start, holdout_start, selected["policy"],
    )
    validation_passed = (
        bool(eligible)
        and 3.5 <= float(validation["base"]["trades_per_day"]) <= 6.0
        and float(validation["base"]["win_rate"]) >= 45.0
        and float(validation["base"]["profit_factor"] or 0) >= 1.1
        and float(validation["stress"]["profit_factor"] or 0) >= 1.0
        and validation["profitable_months"] >= 4
    )
    report: dict[str, Any] = {
        "method": {
            "description": "monthly no-lookahead quality gate for long pullback signals",
            "split": "90d warmup, 120d development, 160d validation, 60d sealed holdout",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "long_branch_full_audit": compact(long_base),
        "development_candidates": candidates,
        "selected_on_development": selected,
        "validation": compact(validation["base"]),
        "validation_stress": compact(validation["stress"]),
        "validation_months": validation["months"],
        "validation_gate_records": validation["gate_records"],
        "validation_passed": validation_passed,
        "holdout_opened": validation_passed,
    }
    if validation_passed:
        holdout = run_period(
            histories, strategy, config["account"], config["broker"], prepared,
            long_base["trade_log"], long_stress["trade_log"],
            holdout_start, data_end, selected["policy"],
        )
        report["final_holdout_once"] = compact(holdout["base"])
        report["final_holdout_stress"] = compact(holdout["stress"])
        report["holdout_months"] = holdout["months"]
        report["holdout_gate_records"] = holdout["gate_records"]
    output = ROOT / "research/adaptive_long_gate_walkforward.json"
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
