#!/usr/bin/env python3
"""Historical Binance Futures backtest for the Crypto Autobot strategy."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
from pathlib import Path
from typing import Any

try:
    from .bot import Candle, adx, atr, ema, evaluate_market_signal, request_json, sma
except ImportError:
    from bot import Candle, adx, atr, ema, evaluate_market_signal, request_json, sma


def interval_ms(interval: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    unit = interval[-1]
    if unit not in units:
        raise ValueError("Backtest supports minute, hour and day intervals.")
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
            )
            if candle.close_time <= end_ms:
                candles.append(candle)
        next_cursor = int(rows[-1][0]) + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(rows) < 1000:
            break
    unique = {candle.open_time: candle for candle in candles}
    return [unique[key] for key in sorted(unique)]


def apply_slippage(price: float, order_side: str, slippage_bps: float) -> float:
    adjustment = slippage_bps / 10_000.0
    return price * (1 + adjustment if order_side == "buy" else 1 - adjustment)


def close_trade(
    position: dict[str, Any],
    raw_price: float,
    reason: str,
    fee_rate: float,
    slippage_bps: float,
) -> dict[str, Any]:
    close_side = "sell" if position["side"] == "long" else "buy"
    price = apply_slippage(raw_price, close_side, slippage_bps)
    qty = float(position["qty"])
    if position["side"] == "long":
        gross = (price - float(position["entry"])) * qty
    else:
        gross = (float(position["entry"]) - price) * qty
    exit_fee = price * qty * fee_rate
    net = gross - float(position["entry_fee"]) - exit_fee
    return {
        "side": position["side"],
        "entry_time": position["entry_time"],
        "exit_time": position["exit_time"],
        "entry": round(float(position["entry"]), 8),
        "exit": round(price, 8),
        "qty": round(qty, 8),
        "stop": round(float(position["stop"]), 8),
        "target": round(float(position["target"]), 8),
        "gross_pnl": round(gross, 2),
        "fees": round(float(position["entry_fee"]) + exit_fee, 2),
        "net_pnl": round(net, 2),
        "reason": reason,
    }


def backtest_symbol(
    candles: list[Candle],
    strategy: dict[str, Any],
    account: dict[str, Any],
    broker: dict[str, Any],
    fee_bps: float,
    slippage_bps: float,
) -> dict[str, Any]:
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    fast = ema(closes, int(strategy["fast_ema"]))
    slow = ema(closes, int(strategy["slow_ema"]))
    atrs = atr(candles, int(strategy["atr_length"]))
    vol_sma = sma(volumes, int(strategy["volume_sma_length"]))
    adxs = adx(candles, int(strategy.get("adx_length", 14)))
    warmup = max(
        int(strategy["slow_ema"]) + int(strategy.get("slow_slope_lookback", 1)),
        int(strategy["breakout_lookback"]) + 2,
        int(strategy["volume_sma_length"]),
        int(strategy["atr_length"]),
    )

    initial_balance = float(account["initial_balance"])
    balance = initial_balance
    peak = initial_balance
    max_drawdown = 0.0
    fee_rate = fee_bps / 10_000.0
    leverage = float(broker.get("leverage", 1))
    same_policy = str(account.get("same_candle_exit", "stop_first"))
    daily: dict[str, dict[str, float]] = {}
    position: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []

    for index in range(warmup, len(candles)):
        candle = candles[index]
        atr_value = atrs[index]
        day = dt.datetime.fromtimestamp(candle.open_time / 1000, dt.timezone.utc).date().isoformat()
        day_stats = daily.setdefault(day, {"trades": 0, "pnl": 0.0})

        if position:
            position["exit_time"] = candle.open_time
            if atr_value:
                if position["side"] == "long":
                    risk = float(position["entry"]) - float(position["initial_stop"])
                    if risk > 0 and candle.high >= float(position["entry"]) + risk * float(strategy["trail_after_r"]):
                        position["stop"] = max(
                            float(position["stop"]),
                            candle.close - float(atr_value) * float(strategy["trail_atr"]),
                        )
                else:
                    risk = float(position["initial_stop"]) - float(position["entry"])
                    if risk > 0 and candle.low <= float(position["entry"]) - risk * float(strategy["trail_after_r"]):
                        position["stop"] = min(
                            float(position["stop"]),
                            candle.close + float(atr_value) * float(strategy["trail_atr"]),
                        )

            if position["side"] == "long":
                hit_stop = candle.low <= float(position["stop"])
                hit_target = candle.high >= float(position["target"])
            else:
                hit_stop = candle.high >= float(position["stop"])
                hit_target = candle.low <= float(position["target"])

            exit_price: float | None = None
            reason = ""
            if hit_stop and hit_target:
                if same_policy == "target_first":
                    exit_price, reason = float(position["target"]), "target_first"
                else:
                    exit_price, reason = float(position["stop"]), "stop_first"
            elif hit_stop:
                exit_price, reason = float(position["stop"]), "stop"
            elif hit_target:
                exit_price, reason = float(position["target"]), "target"

            if exit_price is not None:
                trade = close_trade(position, exit_price, reason, fee_rate, slippage_bps)
                # Entry fee was already removed when the position opened.
                balance += float(trade["net_pnl"]) + float(position["entry_fee"])
                day_stats["pnl"] += float(trade["net_pnl"])
                trades.append(trade)
                position = None
                peak = max(peak, balance)
                max_drawdown = max(max_drawdown, (peak - balance) / peak * 100 if peak else 0)

        if position or atr_value is None:
            continue
        side, reason, _ = evaluate_market_signal(
            candles,
            index,
            strategy,
            fast,
            slow,
            atrs,
            vol_sma,
            adxs,
        )
        if not side:
            continue

        max_loss = initial_balance * float(account["max_daily_loss_percent"]) / 100.0
        if day_stats["trades"] >= int(account["max_daily_trades"]) or day_stats["pnl"] <= -max_loss:
            continue

        order_side = "buy" if side == "long" else "sell"
        entry = apply_slippage(candle.close, order_side, slippage_bps)
        stop_distance = float(atr_value) * float(strategy["stop_atr"])
        target_distance = float(atr_value) * float(strategy["target_atr"])
        risk_cash = balance * float(account["risk_per_trade_percent"]) / 100.0
        qty = risk_cash / stop_distance if stop_distance > 0 else 0
        qty = min(qty, balance * leverage * 0.95 / entry)
        if qty <= 0 or not math.isfinite(qty):
            continue

        stop = entry - stop_distance if side == "long" else entry + stop_distance
        target = entry + target_distance if side == "long" else entry - target_distance
        entry_fee = entry * qty * fee_rate
        balance -= entry_fee
        position = {
            "side": side,
            "entry_time": candle.open_time,
            "exit_time": candle.open_time,
            "entry": entry,
            "qty": qty,
            "stop": stop,
            "initial_stop": stop,
            "target": target,
            "entry_fee": entry_fee,
            "reason": reason,
        }
        day_stats["trades"] += 1

    if position and candles:
        position["exit_time"] = candles[-1].open_time
        trade = close_trade(position, candles[-1].close, "end_of_backtest", fee_rate, slippage_bps)
        balance += float(trade["net_pnl"]) + float(position["entry_fee"])
        trades.append(trade)
        peak = max(peak, balance)
        max_drawdown = max(max_drawdown, (peak - balance) / peak * 100 if peak else 0)

    wins = [trade for trade in trades if float(trade["net_pnl"]) > 0]
    losses = [trade for trade in trades if float(trade["net_pnl"]) < 0]
    gross_profit = sum(float(trade["net_pnl"]) for trade in wins)
    gross_loss = abs(sum(float(trade["net_pnl"]) for trade in losses))
    net_pnl = balance - initial_balance
    return {
        "initial_balance": round(initial_balance, 2),
        "final_balance": round(balance, 2),
        "net_pnl": round(net_pnl, 2),
        "return_percent": round(net_pnl / initial_balance * 100, 2) if initial_balance else 0,
        "max_drawdown_percent": round(max_drawdown, 2),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "trade_log": trades,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the Crypto Autobot strategy.")
    parser.add_argument("--config", default="crypto_autobot/config.example.json")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--fee-bps", type=float, default=5.0, help="commission per side in basis points")
    parser.add_argument("--slippage-bps", type=float, default=2.0, help="slippage per order in basis points")
    parser.add_argument("--output", default="crypto_autobot/data/backtest_report.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.days < 30 or args.days > 1500:
        raise ValueError("--days must be between 30 and 1500.")
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    market = config["market"]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86_400_000
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "days": args.days,
        "interval": market["interval"],
        "fee_bps_per_side": args.fee_bps,
        "slippage_bps_per_order": args.slippage_bps,
        "symbols": {},
        "warning": "Historical results do not guarantee future returns.",
    }
    for symbol in market["symbols"]:
        candles = fetch_history(
            "https://fapi.binance.com",
            str(symbol),
            str(market["interval"]),
            start_ms,
            end_ms,
        )
        result = backtest_symbol(
            candles,
            config["strategy"],
            config["account"],
            config.get("broker", {}),
            args.fee_bps,
            args.slippage_bps,
        )
        result["candles"] = len(candles)
        report["symbols"][str(symbol)] = result
        print(
            f"{symbol}: trades={result['trades']} return={result['return_percent']}% "
            f"maxDD={result['max_drawdown_percent']}% winrate={result['win_rate']}%"
        )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
