#!/usr/bin/env python3
"""No-lookahead audit for a strategy with a rolling liquid-symbol universe."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

try:
    from .bot import Candle
    from .portfolio_backtest import prepare_histories, run_portfolio_backtest
except ImportError:
    from bot import Candle
    from portfolio_backtest import prepare_histories, run_portfolio_backtest


DAY_MS = 86_400_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.demo.regime-scalp.example.json")
    parser.add_argument("--cache-dir", default="data/market_cache_15m_430d")
    parser.add_argument("--output", default="research/rolling_universe_audit.json")
    parser.add_argument("--universe-size", type=int, default=16)
    parser.add_argument("--selection-days", type=int, default=90)
    parser.add_argument("--minimum-trades", type=int, default=8)
    return parser.parse_args()


def load_cached_histories(cache_dir: Path, symbols: list[str], interval: str) -> dict[str, list[Candle]]:
    histories: dict[str, list[Candle]] = {}
    for symbol in symbols:
        matches = sorted(cache_dir.glob(f"{symbol}-{interval}-*.json"))
        if not matches:
            raise FileNotFoundError(f"No cached history for {symbol} {interval} in {cache_dir}")
        rows = json.loads(matches[-1].read_text(encoding="utf-8"))
        histories[symbol] = [Candle(**row) for row in rows]
    return histories


def basic_metrics(trades: list[dict[str, Any]], start_ms: int, end_ms: int) -> dict[str, Any]:
    selected = [trade for trade in trades if start_ms <= int(trade["exit_time"]) < end_ms]
    winners = [trade for trade in selected if float(trade["net_pnl"]) > 0]
    losers = [trade for trade in selected if float(trade["net_pnl"]) < 0]
    gross_profit = sum(float(trade["net_pnl"]) for trade in winners)
    gross_loss = abs(sum(float(trade["net_pnl"]) for trade in losers))
    days = max((end_ms - start_ms) / DAY_MS, 1 / 24)
    return {
        "start_utc": dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc).isoformat(),
        "end_utc": dt.datetime.fromtimestamp(end_ms / 1000, dt.timezone.utc).isoformat(),
        "trades": len(selected),
        "trades_per_day": round(len(selected) / days, 2),
        "win_rate": round(len(winners) / len(selected) * 100, 2) if selected else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "average_realized_r": round(
            sum(float(trade["realized_r"]) for trade in selected) / len(selected), 4
        ) if selected else 0.0,
        "net_pnl": round(sum(float(trade["net_pnl"]) for trade in selected), 2),
    }


def select_periods(
    symbols: list[str],
    individual_trades: dict[str, list[dict[str, Any]]],
    start_ms: int,
    months: int,
    selection_days: int,
    minimum_trades: int,
    universe_size: int,
) -> tuple[list[tuple[int, int, set[str]]], list[dict[str, Any]]]:
    periods: list[tuple[int, int, set[str]]] = []
    records: list[dict[str, Any]] = []
    for month in range(months):
        period_start = start_ms + month * 30 * DAY_MS
        period_end = period_start + 30 * DAY_MS
        calibration_start = period_start - selection_days * DAY_MS
        ranked: list[tuple[float, int, str]] = []
        details: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            calibration = [
                trade
                for trade in individual_trades[symbol]
                if calibration_start <= int(trade["exit_time"]) < period_start
            ]
            count = len(calibration)
            total_r = sum(float(trade["realized_r"]) for trade in calibration)
            score = total_r / (count + 20) if count >= minimum_trades else -999.0
            ranked.append((score, count, symbol))
            details[symbol] = {
                "trades": count,
                "total_r": round(total_r, 4),
                "shrunk_score": round(score, 6),
            }
        ranked.sort(reverse=True)
        chosen = [symbol for score, _, symbol in ranked if score > -999.0][:universe_size]
        if len(chosen) < universe_size:
            chosen.extend(symbol for _, _, symbol in ranked if symbol not in chosen)[: universe_size - len(chosen)]
        chosen_set = set(chosen)
        periods.append((period_start, period_end, chosen_set))
        records.append(
            {
                "start_utc": dt.datetime.fromtimestamp(period_start / 1000, dt.timezone.utc).isoformat(),
                "end_utc": dt.datetime.fromtimestamp(period_end / 1000, dt.timezone.utc).isoformat(),
                "symbols": chosen,
                "selection": {symbol: details[symbol] for symbol in chosen},
            }
        )
    return periods, records


def main() -> int:
    args = parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    strategy = dict(config["strategy"])
    strategy.update(
        {
            "pullback_lookback": 12,
            "long_pullback_rsi": 40.0,
            "short_pullback_rsi": 60.0,
            "min_adx": 12.0,
        }
    )
    symbols = list(config["market"]["symbols"])
    cache_dir = Path(args.cache_dir)
    available_symbols = sorted(path.name.split("-")[0] for path in cache_dir.glob("*-15m-*.json"))
    if available_symbols:
        symbols = available_symbols
    histories = load_cached_histories(cache_dir, symbols, config["market"]["interval"])
    prepared = prepare_histories(histories, strategy)

    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    audit_end = data_end
    audit_start = audit_end - 420 * DAY_MS
    validation_start = audit_start + 90 * DAY_MS
    holdout_start = audit_start + 360 * DAY_MS
    holdout_end = audit_start + 420 * DAY_MS

    account = dict(config["account"])
    calibration_account = dict(account)
    calibration_account.update(
        {
            "max_open_positions": 1,
            "max_daily_trades": 100,
            "max_daily_loss_percent": 100.0,
        }
    )
    broker = dict(config["broker"])
    individual_trades: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        symbol_prepared = ({symbol: prepared[0][symbol]}, set(prepared[0][symbol]["index_by_time"]))
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

    validation_periods, validation_selection = select_periods(
        symbols,
        individual_trades,
        validation_start,
        months=9,
        selection_days=args.selection_days,
        minimum_trades=args.minimum_trades,
        universe_size=args.universe_size,
    )
    holdout_periods, holdout_selection = select_periods(
        symbols,
        individual_trades,
        holdout_start,
        months=2,
        selection_days=args.selection_days,
        minimum_trades=args.minimum_trades,
        universe_size=args.universe_size,
    )

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
    validation = run_portfolio_backtest(
        start_ms=validation_start,
        end_ms=holdout_start,
        active_universe_periods=validation_periods,
        **common,
    )
    holdout = run_portfolio_backtest(
        start_ms=holdout_start,
        end_ms=holdout_end,
        active_universe_periods=holdout_periods,
        **common,
    )

    report = {
        "method": {
            "description": "Monthly top-universe selection using only the previous 90 days",
            "universe_size": args.universe_size,
            "selection_days": args.selection_days,
            "minimum_calibration_trades": args.minimum_trades,
            "selection_score": "sum(realized_r) / (trade_count + 20)",
            "fee_bps": {"maker_entry": 2.0, "taker_exit": 5.0, "exit_slippage": 2.0},
            "same_candle_exit": account.get("same_candle_exit", "stop_first"),
            "strategy": strategy,
        },
        "validation_270d": {key: value for key, value in validation.items() if key != "trade_log"},
        "final_holdout_60d": {key: value for key, value in holdout.items() if key != "trade_log"},
        "validation_months": [
            basic_metrics(validation["trade_log"], start, end) for start, end, _ in validation_periods
        ],
        "holdout_months": [basic_metrics(holdout["trade_log"], start, end) for start, end, _ in holdout_periods],
        "validation_selection": validation_selection,
        "holdout_selection": holdout_selection,
        "trade_logs": {
            "validation": validation["trade_log"],
            "holdout": holdout["trade_log"],
        },
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "validation_270d": {
            key: validation[key]
            for key in ("trades", "trades_per_day", "win_rate", "profit_factor", "average_realized_r", "return_percent", "max_drawdown_percent")
        },
        "final_holdout_60d": {
            key: holdout[key]
            for key in ("trades", "trades_per_day", "win_rate", "profit_factor", "average_realized_r", "return_percent", "max_drawdown_percent")
        },
        "output": str(output_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
