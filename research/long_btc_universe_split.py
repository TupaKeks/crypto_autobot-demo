#!/usr/bin/env python3
"""Three-way audit of a BTC-gated long component across the 15m universe."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import Candle  # noqa: E402
from portfolio_backtest import prepare_histories, run_portfolio_backtest  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics, load_cached_histories  # noqa: E402
from strategy_intraday import build_indicators  # noqa: E402


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


def main() -> int:
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text(encoding="utf-8"))
    strategy = dict(config["strategy"])
    strategy.update({"allow_longs": True, "allow_shorts": False})
    cache_dir = ROOT / "data/market_cache_15m_430d"
    symbols = sorted(path.name.split("-")[0] for path in cache_dir.glob("*-15m-*.json"))
    histories = load_cached_histories(cache_dir, symbols, "15m")
    prepared = prepare_histories(histories, strategy)
    btc = histories["BTCUSDT"]
    btc_index = {candle.open_time: index for index, candle in enumerate(btc)}
    btc_indicators = build_indicators(btc, strategy)

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

    def btc_values(timestamp: int) -> tuple[int, Candle, float, float, float, float] | None:
        index = btc_index.get(timestamp)
        if index is None or index < 96:
            return None
        values = (
            btc_indicators.entry_fast[index],
            btc_indicators.entry_slow[index],
            btc_indicators.regime_fast[index],
            btc_indicators.regime_slow[index],
        )
        if any(value is None for value in values):
            return None
        return index, btc[index], *(float(value) for value in values)

    def regime(_symbol: str, side: str, timestamp: int) -> bool:
        item = btc_values(timestamp)
        return side == "long" and bool(item and item[1].close > item[5] and item[4] > item[5])

    def entries(_symbol: str, side: str, timestamp: int) -> bool:
        item = btc_values(timestamp)
        return side == "long" and bool(
            item and item[1].close > item[2] > item[3] and item[4] > item[5]
        )

    def regime_positive(_symbol: str, side: str, timestamp: int) -> bool:
        item = btc_values(timestamp)
        return side == "long" and bool(
            item
            and item[1].close > item[5]
            and item[4] > item[5]
            and item[1].close > btc[item[0] - 96].close
        )

    gates: dict[str, Callable[[str, str, int], bool]] = {
        "btc_regime": regime,
        "btc_entries_and_regime": entries,
        "btc_regime_positive_24h": regime_positive,
    }
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
    rankings: dict[str, list[dict[str, Any]]] = {}
    for gate_name, gate in gates.items():
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
                signal_filter=gate,
            )
            count = int(result["trades"])
            total_r = sum(float(trade["realized_r"]) for trade in result["trade_log"])
            score = total_r / (count + 30) if count >= 20 else -999.0
            ranking.append((score, symbol, compact(result)))
        ranking.sort(reverse=True)
        rankings[gate_name] = [
            {"symbol": symbol, "score": round(score, 6), "development": metrics}
            for score, symbol, metrics in ranking
        ]
        for size in (8, 12, 16, 20, 24):
            chosen = [symbol for _score, symbol, _metrics in ranking[:size]]
            result = run_portfolio_backtest(
                start_ms=development_start,
                end_ms=validation_start,
                active_universe_periods=[(development_start, validation_start, set(chosen))],
                signal_filter=gate,
                **common,
            )
            months = monthly(result["trade_log"], development_start, 8)
            candidates.append(
                {
                    "gate": gate_name,
                    "symbols": chosen,
                    "development": compact(result),
                    "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
                }
            )

    eligible = [
        item
        for item in candidates
        if 1.5 <= float(item["development"]["trades_per_day"] or 0) <= 3.5
        and float(item["development"]["profit_factor"] or 0) >= 1.1
        and int(item["profitable_months"]) >= 5
    ]
    selected = max(
        eligible or candidates,
        key=lambda item: (
            float(item["development"]["profit_factor"] or 0),
            int(item["profitable_months"]),
        ),
    )
    chosen = set(selected["symbols"])
    selected_gate = gates[str(selected["gate"])]
    active_validation = [(validation_start, holdout_start, chosen)]
    validation = run_portfolio_backtest(
        start_ms=validation_start,
        end_ms=holdout_start,
        active_universe_periods=active_validation,
        signal_filter=selected_gate,
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
        active_universe_periods=active_validation,
        signal_filter=selected_gate,
    )
    validation_months = monthly(validation["trade_log"], validation_start, 4)
    validation_passed = (
        1.5 <= float(validation["trades_per_day"]) <= 3.5
        and float(validation["profit_factor"] or 0) >= 1.1
        and float(validation_stress["profit_factor"] or 0) >= 1.0
        and sum(float(item["net_pnl"]) > 0 for item in validation_months) >= 3
    )
    report: dict[str, Any] = {
        "method": {
            "description": "240d development, 120d validation, sealed 60d holdout",
            "side": "long only, conditioned on closed BTC candles",
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "rankings": rankings,
        "development_candidates": candidates,
        "selected_on_development": selected,
        "validation_120d": compact(validation),
        "validation_stress_maker4_slippage3": compact(validation_stress),
        "validation_months": validation_months,
        "validation_passed": validation_passed,
        "holdout_opened": validation_passed,
    }
    if validation_passed:
        active_holdout = [(holdout_start, data_end, chosen)]
        holdout = run_portfolio_backtest(
            start_ms=holdout_start,
            end_ms=data_end,
            active_universe_periods=active_holdout,
            signal_filter=selected_gate,
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
            active_universe_periods=active_holdout,
            signal_filter=selected_gate,
        )
        report["final_holdout_once"] = compact(holdout)
        report["final_holdout_stress"] = compact(holdout_stress)
        report["holdout_months"] = monthly(holdout["trade_log"], holdout_start, 2)

    output = ROOT / "research/long_btc_universe_split.json"
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
