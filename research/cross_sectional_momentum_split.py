#!/usr/bin/env python3
"""Three-way audit of market-neutral cross-sectional crypto momentum."""

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

from portfolio_backtest import (  # noqa: E402
    close_position,
    create_position,
    prepare_histories,
    slipped,
    summarize,
    utc_day,
)
from rolling_universe import DAY_MS, basic_metrics, load_cached_histories  # noqa: E402

BAR_MS = 15 * 60_000


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


def run_cross_sectional_backtest(
    prepared_data: tuple[dict[str, dict[str, Any]], set[int]],
    strategy: dict[str, Any],
    account: dict[str, Any],
    broker: dict[str, Any],
    start_ms: int,
    end_ms: int,
    maker_fee_bps: float,
    taker_fee_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    prepared, available_times = prepared_data
    times = sorted(time for time in available_times if start_ms <= time < end_ms)
    initial_balance = float(account["initial_balance"])
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0
    risk_percent = float(account["risk_per_trade_percent"])
    leverage = float(broker.get("leverage", 1))
    max_positions = int(account["max_open_positions"])
    max_daily_trades = int(account["max_daily_trades"])
    max_daily_loss = initial_balance * float(account["max_daily_loss_percent"]) / 100
    maker_rate = maker_fee_bps / 10_000
    taker_rate = taker_fee_bps / 10_000
    formation_bars = int(strategy["formation_bars"])
    rebalance_bars = int(strategy["rebalance_bars"])
    holding_bars = int(strategy["max_holding_bars"])
    direction = str(strategy["cross_sectional_direction"])
    positions: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    daily: dict[str, dict[str, float]] = {}
    execution = {"signals": 0, "market_entries": 0, "target_limit_fills": 0}

    for timestamp in times:
        day_item = daily.setdefault(utc_day(timestamp), {"trades": 0, "pnl": 0.0})
        if pending:
            for order in pending:
                if (
                    order["symbol"] in positions
                    or len(positions) >= max_positions
                    or int(day_item["trades"]) >= max_daily_trades
                    or float(day_item["pnl"]) <= -max_daily_loss
                ):
                    continue
                symbol_data = prepared[order["symbol"]]
                index = symbol_data["index_by_time"].get(timestamp)
                if index is None:
                    continue
                candle = symbol_data["candles"][index]
                order_side = "buy" if order["side"] == "long" else "sell"
                entry = slipped(candle.open, order_side, slippage_bps)
                position = create_position(
                    order["symbol"],
                    order["side"],
                    entry,
                    order["atr_value"],
                    strategy,
                    balance,
                    risk_percent,
                    leverage,
                    taker_rate,
                    timestamp,
                    index,
                    "cross-sectional momentum",
                )
                if position is None:
                    continue
                positions[order["symbol"]] = position
                balance -= float(position["entry_fee"])
                day_item["trades"] += 1
                execution["market_entries"] += 1
            pending = []

        for symbol, position in list(positions.items()):
            symbol_data = prepared[symbol]
            index = symbol_data["index_by_time"].get(timestamp)
            if index is None or index <= int(position["entry_index"]):
                continue
            candle = symbol_data["candles"][index]
            position["bars_held"] = int(position["bars_held"]) + 1
            if position["side"] == "long":
                hit_stop = candle.low <= float(position["stop"])
                hit_target = candle.high >= float(position["target"])
            else:
                hit_stop = candle.high >= float(position["stop"])
                hit_target = candle.low <= float(position["target"])
            raw_exit: float | None = None
            reason = ""
            if hit_stop:
                raw_exit, reason = float(position["stop"]), "stop"
            elif hit_target:
                raw_exit, reason = float(position["target"]), "target"
            elif int(position["bars_held"]) >= holding_bars:
                raw_exit, reason = candle.close, "time_exit"
            if raw_exit is None:
                continue
            target_fill = reason == "target"
            trade = close_position(
                position,
                raw_exit,
                reason,
                timestamp,
                maker_rate if target_fill else taker_rate,
                0.0 if target_fill else slippage_bps,
            )
            if target_fill:
                execution["target_limit_fills"] += 1
            balance += float(trade["gross_pnl"]) - (float(trade["fees"]) - float(position["entry_fee"]))
            day_item["pnl"] += float(trade["net_pnl"])
            trades.append(trade)
            positions.pop(symbol)
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, (peak - balance) / peak * 100 if peak else 0.0)

        if timestamp % (rebalance_bars * BAR_MS) != 0:
            continue
        ranked: list[tuple[float, str, float]] = []
        for symbol, symbol_data in prepared.items():
            if symbol in positions:
                continue
            index = symbol_data["index_by_time"].get(timestamp)
            if index is None or index < formation_bars:
                continue
            atr_value = symbol_data["indicators"].atr[index]
            if atr_value is None:
                continue
            current = symbol_data["candles"][index].close
            past = symbol_data["candles"][index - formation_bars].close
            if past <= 0:
                continue
            ranked.append((current / past - 1.0, symbol, float(atr_value)))
        if len(ranked) < 2:
            continue
        ranked.sort()
        loser = ranked[0]
        winner = ranked[-1]
        if direction == "momentum":
            selections = ((winner, "long"), (loser, "short"))
        else:
            selections = ((winner, "short"), (loser, "long"))
        pending = [
            {"symbol": item[1], "side": side, "atr_value": item[2]}
            for item, side in selections
            if item[1] not in positions
        ]
        execution["signals"] += len(pending)

    for symbol, position in list(positions.items()):
        candles = prepared[symbol]["candles"]
        final = next((candle for candle in reversed(candles) if candle.open_time < end_ms), None)
        if final is None:
            continue
        trade = close_position(position, final.close, "end_of_period", final.open_time, taker_rate, slippage_bps)
        balance += float(trade["gross_pnl"]) - (float(trade["fees"]) - float(position["entry_fee"]))
        trades.append(trade)
    period_days = max((end_ms - start_ms) / DAY_MS, 1 / 24)
    return summarize(trades, initial_balance, balance, max_drawdown, period_days, strategy, execution)


def main() -> int:
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text(encoding="utf-8"))
    cache_dir = ROOT / "data/market_cache_15m_430d"
    symbols = sorted(path.name.split("-")[0] for path in cache_dir.glob("*-15m-*.json"))
    histories = load_cached_histories(cache_dir, symbols, "15m")
    indicator_strategy = dict(config["strategy"])
    prepared = prepare_histories(histories, indicator_strategy)
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    development_start = data_end - 420 * DAY_MS
    validation_start = data_end - 180 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS
    account = dict(config["account"])
    account.update({"max_open_positions": 2, "max_daily_trades": 12})
    broker = dict(config["broker"])

    candidates: list[dict[str, Any]] = []
    for formation_hours, rebalance_hours, direction, distances in itertools.product(
        (6, 12, 24, 48),
        (6, 8, 12),
        ("momentum", "reversal"),
        ((1.8, 2.8), (2.4, 3.8)),
    ):
        strategy = dict(indicator_strategy)
        strategy.update(
            {
                "formation_bars": formation_hours * 4,
                "rebalance_bars": rebalance_hours * 4,
                "max_holding_bars": rebalance_hours * 4,
                "cross_sectional_direction": direction,
                "stop_atr": distances[0],
                "target_atr": distances[1],
            }
        )
        result = run_cross_sectional_backtest(
            prepared,
            strategy,
            account,
            broker,
            development_start,
            validation_start,
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
            slippage_bps=2.0,
        )
        months = monthly(result["trade_log"], development_start, 8)
        candidates.append(
            {
                "parameters": {
                    "formation_hours": formation_hours,
                    "rebalance_hours": rebalance_hours,
                    "direction": direction,
                    "stop_atr": distances[0],
                    "target_atr": distances[1],
                },
                "development": compact(result),
                "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
            }
        )

    eligible = [
        item
        for item in candidates
        if 3.5 <= float(item["development"]["trades_per_day"] or 0) <= 6.5
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
    p = selected["parameters"]
    strategy = dict(indicator_strategy)
    strategy.update(
        {
            "formation_bars": int(p["formation_hours"]) * 4,
            "rebalance_bars": int(p["rebalance_hours"]) * 4,
            "max_holding_bars": int(p["rebalance_hours"]) * 4,
            "cross_sectional_direction": p["direction"],
            "stop_atr": p["stop_atr"],
            "target_atr": p["target_atr"],
        }
    )
    validation = run_cross_sectional_backtest(
        prepared,
        strategy,
        account,
        broker,
        validation_start,
        holdout_start,
        maker_fee_bps=2.0,
        taker_fee_bps=5.0,
        slippage_bps=2.0,
    )
    validation_stress = run_cross_sectional_backtest(
        prepared,
        strategy,
        account,
        broker,
        validation_start,
        holdout_start,
        maker_fee_bps=4.0,
        taker_fee_bps=7.0,
        slippage_bps=4.0,
    )
    validation_months = monthly(validation["trade_log"], validation_start, 4)
    validation_passed = (
        3.5 <= float(validation["trades_per_day"]) <= 6.5
        and float(validation["profit_factor"] or 0) >= 1.1
        and float(validation_stress["profit_factor"] or 0) >= 1.0
        and sum(float(item["net_pnl"]) > 0 for item in validation_months) >= 3
    )
    report: dict[str, Any] = {
        "method": {
            "description": "240d development, 120d validation, sealed 60d holdout",
            "entry": "rank on closed 15m candle, market entry at next candle open",
            "symbols": symbols,
            "candidate_count": len(candidates),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "selected_on_development": selected,
        "strategy": strategy,
        "validation_120d": compact(validation),
        "validation_stress": compact(validation_stress),
        "validation_months": validation_months,
        "validation_passed": validation_passed,
        "holdout_opened": validation_passed,
        "development_candidates": candidates,
    }
    if validation_passed:
        holdout = run_cross_sectional_backtest(
            prepared,
            strategy,
            account,
            broker,
            holdout_start,
            data_end,
            maker_fee_bps=2.0,
            taker_fee_bps=5.0,
            slippage_bps=2.0,
        )
        holdout_stress = run_cross_sectional_backtest(
            prepared,
            strategy,
            account,
            broker,
            holdout_start,
            data_end,
            maker_fee_bps=4.0,
            taker_fee_bps=7.0,
            slippage_bps=4.0,
        )
        report["final_holdout_once"] = compact(holdout)
        report["final_holdout_stress"] = compact(holdout_stress)
        report["holdout_months"] = monthly(holdout["trade_log"], holdout_start, 2)

    output = ROOT / "research/cross_sectional_momentum_split.json"
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
