#!/usr/bin/env python3
"""Portfolio-aware walk-forward backtest for the intraday strategy."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
import time
from pathlib import Path
from typing import Any, Callable

try:
    from .bot import Candle, request_json
    from .strategy_intraday import build_indicators, evaluate_strategy_signal, minimum_history
except ImportError:
    from bot import Candle, request_json
    from strategy_intraday import build_indicators, evaluate_strategy_signal, minimum_history


def interval_ms(interval: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    unit = interval[-1]
    if unit not in units:
        raise ValueError("Supported intervals use m, h or d suffixes.")
    return int(interval[:-1]) * units[unit]


def fetch_history(
    base_url: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[Candle]:
    candles: list[Candle] = []
    cursor = start_ms
    step = interval_ms(interval)
    while cursor < end_ms:
        rows = request_json(
            f"{base_url.rstrip('/')}/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not rows:
            break
        for row in rows:
            candle = Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                close_time=int(row[6]),
                quote_volume=float(row[7]) if len(row) > 7 else 0.0,
                trade_count=int(row[8]) if len(row) > 8 else 0,
                taker_buy_volume=float(row[9]) if len(row) > 9 else 0.0,
                taker_buy_quote_volume=float(row[10]) if len(row) > 10 else 0.0,
            )
            if candle.close_time <= end_ms:
                candles.append(candle)
        next_cursor = int(rows[-1][0]) + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(rows) < 1000:
            break
        # LIMIT=1000 costs five request-weight units; this keeps bulk research
        # downloads below the documented 2400 weight/minute ceiling.
        time.sleep(0.14)
    unique = {candle.open_time: candle for candle in candles}
    return [unique[key] for key in sorted(unique)]


def load_history(
    cache_dir: Path,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[Candle]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol}-{interval}-{start_ms}-{end_ms}.json"
    if cache_path.exists():
        rows = json.loads(cache_path.read_text(encoding="utf-8"))
        return [Candle(**row) for row in rows]
    candles = fetch_history("https://fapi.binance.com", symbol, interval, start_ms, end_ms)
    cache_path.write_text(
        json.dumps([dataclasses.asdict(candle) for candle in candles]),
        encoding="utf-8",
    )
    return candles


def slipped(price: float, order_side: str, slippage_bps: float) -> float:
    amount = slippage_bps / 10_000.0
    return price * (1 + amount if order_side == "buy" else 1 - amount)


def utc_day(timestamp_ms: int) -> str:
    return dt.datetime.fromtimestamp(timestamp_ms / 1000, dt.timezone.utc).date().isoformat()


def close_position(
    position: dict[str, Any],
    raw_exit: float,
    reason: str,
    timestamp: int,
    exit_fee_rate: float,
    slippage_bps: float,
) -> dict[str, Any]:
    exit_side = "sell" if position["side"] == "long" else "buy"
    exit_price = slipped(raw_exit, exit_side, slippage_bps)
    quantity = float(position["quantity"])
    if position["side"] == "long":
        gross = (exit_price - float(position["entry"])) * quantity
    else:
        gross = (float(position["entry"]) - exit_price) * quantity
    exit_fee = exit_price * quantity * exit_fee_rate
    net = gross - float(position["entry_fee"]) - exit_fee
    risk_cash = float(position["risk_cash"])
    return {
        "symbol": position["symbol"],
        "side": position["side"],
        "entry_time": position["entry_time"],
        "exit_time": timestamp,
        "entry": round(float(position["entry"]), 8),
        "exit": round(exit_price, 8),
        "stop": round(float(position["stop"]), 8),
        "target": round(float(position["target"]), 8),
        "quantity": round(quantity, 8),
        "gross_pnl": round(gross, 4),
        "fees": round(float(position["entry_fee"]) + exit_fee, 4),
        "net_pnl": round(net, 4),
        "realized_r": round(net / risk_cash, 4) if risk_cash else 0.0,
        "bars_held": int(position["bars_held"]),
        "reason": reason,
    }


def summarize(
    trades: list[dict[str, Any]],
    initial_balance: float,
    final_balance: float,
    max_drawdown: float,
    period_days: float,
    strategy: dict[str, Any],
    execution: dict[str, int] | None = None,
) -> dict[str, Any]:
    wins = [trade for trade in trades if float(trade["net_pnl"]) > 0]
    losses = [trade for trade in trades if float(trade["net_pnl"]) < 0]
    gross_profit = sum(float(trade["net_pnl"]) for trade in wins)
    gross_loss = abs(sum(float(trade["net_pnl"]) for trade in losses))
    realized_rs = [float(trade["realized_r"]) for trade in trades]
    by_symbol: dict[str, dict[str, float]] = {}
    for trade in trades:
        item = by_symbol.setdefault(trade["symbol"], {"trades": 0, "net_pnl": 0.0, "wins": 0})
        item["trades"] += 1
        item["net_pnl"] += float(trade["net_pnl"])
        item["wins"] += int(float(trade["net_pnl"]) > 0)
    for item in by_symbol.values():
        trades_count = int(item["trades"])
        item["net_pnl"] = round(item["net_pnl"], 2)
        item["win_rate"] = round(float(item.pop("wins")) / trades_count * 100, 2) if trades_count else 0.0

    return {
        "initial_balance": round(initial_balance, 2),
        "final_balance": round(final_balance, 2),
        "net_pnl": round(final_balance - initial_balance, 2),
        "return_percent": round((final_balance / initial_balance - 1) * 100, 2) if initial_balance else 0.0,
        "max_drawdown_percent": round(max_drawdown, 2),
        "trades": len(trades),
        "trades_per_day": round(len(trades) / period_days, 2) if period_days else 0.0,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "target_reward_risk": round(float(strategy["target_atr"]) / float(strategy["stop_atr"]), 2),
        "average_realized_r": round(sum(realized_rs) / len(realized_rs), 4) if realized_rs else 0.0,
        "average_win_r": round(sum(float(item["realized_r"]) for item in wins) / len(wins), 4) if wins else 0.0,
        "average_loss_r": round(sum(float(item["realized_r"]) for item in losses) / len(losses), 4) if losses else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "execution": execution or {},
        "by_symbol": by_symbol,
        "trade_log": trades,
    }


def create_position(
    symbol: str,
    side: str,
    entry: float,
    atr_value: float,
    strategy: dict[str, Any],
    balance: float,
    risk_percent: float,
    leverage: float,
    entry_fee_rate: float,
    timestamp: int,
    entry_index: int,
    reason: str,
) -> dict[str, Any] | None:
    stop_distance = atr_value * float(strategy["stop_atr"])
    target_distance = atr_value * float(strategy["target_atr"])
    risk_cash = balance * risk_percent / 100
    quantity = risk_cash / stop_distance if stop_distance > 0 else 0.0
    quantity = min(quantity, balance * leverage * 0.95 / entry)
    if quantity <= 0 or not math.isfinite(quantity):
        return None
    stop = entry - stop_distance if side == "long" else entry + stop_distance
    target = entry + target_distance if side == "long" else entry - target_distance
    entry_fee = entry * quantity * entry_fee_rate
    return {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "stop": stop,
        "target": target,
        "quantity": quantity,
        "entry_fee": entry_fee,
        "risk_cash": risk_cash,
        "entry_time": timestamp,
        "entry_index": entry_index,
        "bars_held": 0,
        "reason": reason,
    }


def prepare_histories(
    histories: dict[str, list[Candle]],
    strategy: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], set[int]]:
    prepared: dict[str, dict[str, Any]] = {}
    all_times: set[int] = set()
    for symbol, candles in histories.items():
        index_by_time = {candle.open_time: index for index, candle in enumerate(candles)}
        prepared[symbol] = {
            "candles": candles,
            "indicators": build_indicators(candles, strategy),
            "index_by_time": index_by_time,
        }
        all_times.update(index_by_time)
    return prepared, all_times


def run_portfolio_backtest(
    histories: dict[str, list[Candle]],
    strategy: dict[str, Any],
    account: dict[str, Any],
    broker: dict[str, Any],
    start_ms: int,
    end_ms: int,
    fee_bps: float,
    slippage_bps: float,
    prepared_data: tuple[dict[str, dict[str, Any]], set[int]] | None = None,
    maker_fee_bps: float | None = None,
    active_universe_periods: list[tuple[int, int, set[str]]] | None = None,
    signal_filter: Callable[[str, str, int], bool] | None = None,
) -> dict[str, Any]:
    prepared, available_times = prepared_data or prepare_histories(histories, strategy)
    all_times = {time_value for time_value in available_times if start_ms <= time_value < end_ms}

    initial_balance = float(account["initial_balance"])
    balance = initial_balance
    peak = initial_balance
    max_drawdown = 0.0
    taker_fee_rate = fee_bps / 10_000.0
    maker_fee_rate = (fee_bps if maker_fee_bps is None else maker_fee_bps) / 10_000.0
    leverage = float(broker.get("leverage", 1))
    max_positions = int(account["max_open_positions"])
    max_daily_trades = int(account["max_daily_trades"])
    max_daily_loss = initial_balance * float(account["max_daily_loss_percent"]) / 100
    default_risk_percent = float(account["risk_per_trade_percent"])

    def risk_for_side(side: str) -> float:
        key = "long_risk_per_trade_percent" if side == "long" else "short_risk_per_trade_percent"
        return float(account.get(key, default_risk_percent))
    same_candle_exit = str(account.get("same_candle_exit", "stop_first"))
    max_holding = int(strategy.get("max_holding_bars", 0))
    cooldown = int(strategy.get("cooldown_bars", 0))
    entry_order_type = str(strategy.get("entry_order_type", "market"))
    target_order_type = str(strategy.get("target_order_type", "market"))
    if target_order_type not in ("market", "limit"):
        raise ValueError("target_order_type must be market or limit.")
    entry_offset_atr = float(strategy.get("entry_offset_atr", 0.0))
    entry_expiry_bars = max(1, int(strategy.get("entry_expiry_bars", 1)))
    positions: dict[str, dict[str, Any]] = {}
    pending_orders: dict[str, dict[str, Any]] = {}
    last_exit_index: dict[str, int] = {}
    daily: dict[str, dict[str, float]] = {}
    trades: list[dict[str, Any]] = []
    execution = {
        "signals": 0,
        "limit_orders": 0,
        "limit_fills": 0,
        "limit_expired": 0,
        "maker_target_fills": 0,
    }

    def active_symbols(timestamp: int) -> set[str]:
        if active_universe_periods is None:
            return set(prepared)
        for period_start, period_end, symbols in active_universe_periods:
            if period_start <= timestamp < period_end:
                return symbols
        return set()

    for timestamp in sorted(all_times):
        eligible_symbols = active_symbols(timestamp)
        for symbol, order in sorted(
            list(pending_orders.items()),
            key=lambda item: float(item[1]["strength"]),
            reverse=True,
        ):
            if symbol not in eligible_symbols:
                pending_orders.pop(symbol)
                execution["limit_expired"] += 1
                continue
            symbol_data = prepared[symbol]
            index = symbol_data["index_by_time"].get(timestamp)
            if index is None or index <= int(order["signal_index"]):
                continue
            if index > int(order["signal_index"]) + entry_expiry_bars:
                pending_orders.pop(symbol)
                execution["limit_expired"] += 1
                continue
            candle = symbol_data["candles"][index]
            limit_price = float(order["limit_price"])
            touched = candle.low <= limit_price if order["side"] == "long" else candle.high >= limit_price
            if not touched:
                continue
            day = utc_day(timestamp)
            daily_item = daily.setdefault(day, {"trades": 0, "pnl": 0.0})
            if (
                len(positions) >= max_positions
                or int(daily_item["trades"]) >= max_daily_trades
                or float(daily_item["pnl"]) <= -max_daily_loss
            ):
                pending_orders.pop(symbol)
                continue
            position = create_position(
                symbol,
                str(order["side"]),
                limit_price,
                float(order["atr_value"]),
                strategy,
                balance,
                risk_for_side(str(order["side"])),
                leverage,
                maker_fee_rate,
                timestamp,
                int(order["signal_index"]),
                str(order["reason"]),
            )
            pending_orders.pop(symbol)
            if position is None:
                continue
            positions[symbol] = position
            balance -= float(position["entry_fee"])
            daily_item["trades"] += 1
            execution["limit_fills"] += 1

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
            if hit_stop and hit_target:
                if same_candle_exit == "target_first":
                    raw_exit, reason = float(position["target"]), "target_first"
                else:
                    raw_exit, reason = float(position["stop"]), "stop_first"
            elif hit_stop:
                raw_exit, reason = float(position["stop"]), "stop"
            elif hit_target:
                raw_exit, reason = float(position["target"]), "target"
            elif max_holding and int(position["bars_held"]) >= max_holding:
                raw_exit, reason = candle.close, "time_exit"
            if raw_exit is None:
                continue

            target_fill = target_order_type == "limit" and reason in ("target", "target_first")
            trade = close_position(
                position,
                raw_exit,
                reason,
                timestamp,
                maker_fee_rate if target_fill else taker_fee_rate,
                0.0 if target_fill else slippage_bps,
            )
            if target_fill:
                execution["maker_target_fills"] += 1
            balance += float(trade["gross_pnl"]) - (float(trade["fees"]) - float(position["entry_fee"]))
            daily_item = daily.setdefault(utc_day(timestamp), {"trades": 0, "pnl": 0.0})
            daily_item["pnl"] += float(trade["net_pnl"])
            trades.append(trade)
            positions.pop(symbol)
            last_exit_index[symbol] = index
            peak = max(peak, balance)
            if peak:
                max_drawdown = max(max_drawdown, (peak - balance) / peak * 100)

        candidates: list[tuple[float, str, int, Any]] = []
        for symbol, symbol_data in prepared.items():
            if symbol not in eligible_symbols:
                continue
            if symbol in positions or symbol in pending_orders:
                continue
            index = symbol_data["index_by_time"].get(timestamp)
            if index is None or index < minimum_history(strategy):
                continue
            if index - last_exit_index.get(symbol, -10_000) <= cooldown:
                continue
            decision = evaluate_strategy_signal(
                symbol_data["candles"],
                index,
                strategy,
                symbol_data["indicators"],
            )
            if (
                decision.side
                and decision.atr_value
                and (signal_filter is None or signal_filter(symbol, decision.side, timestamp))
            ):
                candidates.append((decision.strength, symbol, index, decision))
                execution["signals"] += 1

        for strength, symbol, index, decision in sorted(candidates, reverse=True):
            if entry_order_type == "market" and len(positions) >= max_positions:
                break
            day = utc_day(timestamp)
            daily_item = daily.setdefault(day, {"trades": 0, "pnl": 0.0})
            if int(daily_item["trades"]) >= max_daily_trades or float(daily_item["pnl"]) <= -max_daily_loss:
                continue
            candle = prepared[symbol]["candles"][index]
            if entry_order_type == "limit_retrace":
                offset = float(decision.atr_value) * entry_offset_atr
                limit_price = candle.close - offset if decision.side == "long" else candle.close + offset
                pending_orders[symbol] = {
                    "symbol": symbol,
                    "side": decision.side,
                    "limit_price": limit_price,
                    "atr_value": float(decision.atr_value),
                    "signal_time": timestamp,
                    "signal_index": index,
                    "strength": strength,
                    "reason": decision.reason,
                }
                execution["limit_orders"] += 1
                continue
            order_side = "buy" if decision.side == "long" else "sell"
            entry = slipped(candle.close, order_side, slippage_bps)
            position = create_position(
                symbol,
                decision.side,
                entry,
                float(decision.atr_value),
                strategy,
                balance,
                risk_for_side(decision.side),
                leverage,
                taker_fee_rate,
                timestamp,
                index,
                decision.reason,
            )
            if position is None:
                continue
            balance -= float(position["entry_fee"])
            positions[symbol] = position
            daily_item["trades"] += 1

    for symbol, position in list(positions.items()):
        candles = prepared[symbol]["candles"]
        final_candidates = [candle for candle in candles if candle.open_time < end_ms]
        if not final_candidates:
            continue
        candle = final_candidates[-1]
        trade = close_position(position, candle.close, "end_of_period", candle.open_time, taker_fee_rate, slippage_bps)
        balance += float(trade["gross_pnl"]) - (float(trade["fees"]) - float(position["entry_fee"]))
        trades.append(trade)

    period_days = max((end_ms - start_ms) / 86_400_000, 1 / 24)
    return summarize(trades, initial_balance, balance, max_drawdown, period_days, strategy, execution)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward portfolio backtest.")
    parser.add_argument("--config", default="crypto_autobot/config.demo.intraday.example.json")
    parser.add_argument("--days", type=int, default=240)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--maker-fee-bps", type=float, default=2.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--cache-dir", default="crypto_autobot/data/market_cache")
    parser.add_argument("--output", default="crypto_autobot/data/walkforward_report.json")
    return parser.parse_args()


def compact_metrics(result: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "return_percent",
        "max_drawdown_percent",
        "trades",
        "trades_per_day",
        "win_rate",
        "target_reward_risk",
        "average_realized_r",
        "profit_factor",
    )
    return {key: result[key] for key in keys}


def main() -> int:
    args = parse_args()
    if args.days < 60 or args.test_days < 20 or args.test_days >= args.days:
        raise ValueError("Use at least 60 total days, 20 test days, and test-days < days.")
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    end_ms = int(time.time() * 1000)
    overall_start = end_ms - args.days * 86_400_000
    split_ms = end_ms - args.test_days * 86_400_000
    warmup_ms = max(minimum_history(config["strategy"]) * interval_ms(config["market"]["interval"]), 7 * 86_400_000)
    fetch_start = overall_start - warmup_ms
    histories = {
        symbol: load_history(
            Path(args.cache_dir),
            symbol,
            config["market"]["interval"],
            fetch_start,
            end_ms,
        )
        for symbol in config["market"]["symbols"]
    }
    train = run_portfolio_backtest(
        histories,
        config["strategy"],
        config["account"],
        config["broker"],
        overall_start,
        split_ms,
        args.fee_bps,
        args.slippage_bps,
        maker_fee_bps=args.maker_fee_bps,
    )
    test = run_portfolio_backtest(
        histories,
        config["strategy"],
        config["account"],
        config["broker"],
        split_ms,
        end_ms,
        args.fee_bps,
        args.slippage_bps,
        maker_fee_bps=args.maker_fee_bps,
    )
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "interval": config["market"]["interval"],
        "symbols": config["market"]["symbols"],
        "train_days": args.days - args.test_days,
        "test_days": args.test_days,
        "fee_bps_per_side": args.fee_bps,
        "maker_fee_bps_per_side": args.maker_fee_bps,
        "slippage_bps_per_order": args.slippage_bps,
        "strategy": config["strategy"],
        "train": train,
        "test": test,
        "warning": "Historical results do not guarantee future returns.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("TRAIN", json.dumps(compact_metrics(train), sort_keys=True))
    print("TEST ", json.dumps(compact_metrics(test), sort_keys=True))
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
