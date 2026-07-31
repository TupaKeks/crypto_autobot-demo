#!/usr/bin/env python3
"""24/7 crypto strategy bot with paper, Binance Demo and live modes."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import json
import math
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .binance_futures import BinanceFuturesBroker, LIVE_CONFIRMATION
except ImportError:
    from binance_futures import BinanceFuturesBroker, LIVE_CONFIRMATION


DEMO_TEST_CONFIRMATION = "DEMO_MARKET_TEST"


@dataclasses.dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    @property
    def open_dt(self) -> str:
        return dt.datetime.fromtimestamp(self.open_time / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@dataclasses.dataclass
class BotContext:
    config: dict[str, Any]
    state_path: Path
    trades_path: Path
    timezone: ZoneInfo
    mode: str
    broker: BinanceFuturesBroker | None
    orders_enabled: bool
    exchange_snapshot: dict[str, Any]
    lock: threading.Lock
    stop_event: threading.Event


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    env_port = os.environ.get("PORT")
    if env_port:
        config.setdefault("app", {})["port"] = int(env_port)
    env_host = os.environ.get("HOST")
    if env_host:
        config.setdefault("app", {})["host"] = env_host
    return config


def now_iso(timezone: ZoneInfo) -> str:
    return dt.datetime.now(timezone).isoformat(timespec="seconds")


def today_key(timezone: ZoneInfo) -> str:
    return dt.datetime.now(timezone).date().isoformat()


def ensure_state(ctx: BotContext) -> dict[str, Any]:
    ctx.state_path.parent.mkdir(parents=True, exist_ok=True)
    if ctx.state_path.exists():
        with ctx.state_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    balance = float(ctx.config["account"]["initial_balance"])
    state = {
        "created_at": now_iso(ctx.timezone),
        "updated_at": now_iso(ctx.timezone),
        "mode": ctx.mode,
        "balance": balance,
        "initial_balance": balance,
        "realized_pnl": 0.0,
        "positions": {},
        "exchange_positions": {},
        "broker_status": {
            "mode": ctx.mode,
            "connected": ctx.mode == "paper",
            "orders_enabled": ctx.orders_enabled,
        },
        "trades": [],
        "daily": {},
        "seen_signal_candles": {},
        "latest": {},
        "logs": [],
    }
    write_state(ctx, state)
    return state


def write_state(ctx: BotContext, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso(ctx.timezone)
    tmp = ctx.state_path.with_suffix(ctx.state_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(ctx.state_path)


def ensure_trades_file(ctx: BotContext) -> None:
    ctx.trades_path.parent.mkdir(parents=True, exist_ok=True)
    if ctx.trades_path.exists():
        return
    with ctx.trades_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "time",
                "event",
                "symbol",
                "side",
                "qty",
                "price",
                "stop",
                "target",
                "pnl",
                "balance",
                "reason",
            ]
        )


def log_event(state: dict[str, Any], message: str, timezone: ZoneInfo) -> None:
    state.setdefault("logs", []).append({"time": now_iso(timezone), "message": message})
    state["logs"] = state["logs"][-120:]


def append_trade(ctx: BotContext, state: dict[str, Any], row: dict[str, Any]) -> None:
    state.setdefault("trades", []).append(row)
    state["trades"] = state["trades"][-500:]
    with ctx.trades_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                row.get("time"),
                row.get("event"),
                row.get("symbol"),
                row.get("side"),
                row.get("qty"),
                row.get("price"),
                row.get("stop"),
                row.get("target"),
                row.get("pnl"),
                row.get("balance"),
                row.get("reason"),
            ]
        )


def daily_stats(state: dict[str, Any], ctx: BotContext) -> dict[str, Any]:
    key = today_key(ctx.timezone)
    state.setdefault("daily", {}).setdefault(key, {"trades": 0, "realized_pnl": 0.0})
    return state["daily"][key]


def request_json(url: str, params: dict[str, Any] | None = None) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "crypto-autobot/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def send_notification(ctx: BotContext, text: str) -> None:
    notifications = ctx.config.get("notifications", {})
    if not notifications.get("telegram_enabled", False):
        return
    token = os.environ.get(str(notifications.get("token_env", "TELEGRAM_BOT_TOKEN")))
    chat_id = os.environ.get(str(notifications.get("chat_id_env", "TELEGRAM_CHAT_ID")))
    if not token or not chat_id:
        return
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def notify_safely(ctx: BotContext, state: dict[str, Any], text: str) -> None:
    try:
        send_notification(ctx, text)
    except Exception as exc:  # noqa: BLE001
        log_event(state, f"Telegram notification failed: {exc}", ctx.timezone)


def fetch_klines(base_url: str, symbol: str, interval: str, limit: int) -> list[Candle]:
    path = "/fapi/v1/klines" if "fapi.binance.com" in base_url else "/api/v3/klines"
    rows = request_json(
        f"{base_url.rstrip('/')}{path}",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    now_ms = int(time.time() * 1000)
    candles: list[Candle] = []
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
        if candle.close_time <= now_ms:
            candles.append(candle)
    return candles


def sma(values: list[float], length: int) -> list[float | None]:
    result: list[float | None] = []
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= length:
            total -= values[i - length]
        result.append(total / length if i >= length - 1 else None)
    return result


def ema(values: list[float], length: int) -> list[float | None]:
    result: list[float | None] = []
    alpha = 2.0 / (length + 1)
    current: float | None = None
    for i, value in enumerate(values):
        if i < length - 1:
            result.append(None)
            continue
        if current is None:
            current = sum(values[i - length + 1 : i + 1]) / length
        else:
            current = value * alpha + current * (1 - alpha)
        result.append(current)
    return result


def atr(candles: list[Candle], length: int) -> list[float | None]:
    ranges: list[float] = []
    prev_close: float | None = None
    for candle in candles:
        if prev_close is None:
            ranges.append(candle.high - candle.low)
        else:
            ranges.append(max(candle.high - candle.low, abs(candle.high - prev_close), abs(candle.low - prev_close)))
        prev_close = candle.close
    return sma(ranges, length)


def wilder_rma(values: list[float], length: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < length:
        return result
    current = sum(values[:length]) / length
    result[length - 1] = current
    for index in range(length, len(values)):
        current = (current * (length - 1) + values[index]) / length
        result[index] = current
    return result


def adx(candles: list[Candle], length: int) -> list[float | None]:
    if not candles:
        return []
    true_ranges = [candles[0].high - candles[0].low]
    plus_dm = [0.0]
    minus_dm = [0.0]
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        up_move = current.high - previous.high
        down_move = previous.low - current.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )

    tr_smoothed = wilder_rma(true_ranges, length)
    plus_smoothed = wilder_rma(plus_dm, length)
    minus_smoothed = wilder_rma(minus_dm, length)
    dx: list[float | None] = [None] * len(candles)
    for index in range(len(candles)):
        if tr_smoothed[index] is None or not tr_smoothed[index]:
            continue
        plus_di = 100 * float(plus_smoothed[index]) / float(tr_smoothed[index])
        minus_di = 100 * float(minus_smoothed[index]) / float(tr_smoothed[index])
        total = plus_di + minus_di
        if total:
            dx[index] = 100 * abs(plus_di - minus_di) / total

    result: list[float | None] = [None] * len(candles)
    valid = [(index, value) for index, value in enumerate(dx) if value is not None]
    if len(valid) < length:
        return result
    first_index = valid[length - 1][0]
    current_adx = sum(float(value) for _, value in valid[:length]) / length
    result[first_index] = current_adx
    for index, value in valid[length:]:
        current_adx = (current_adx * (length - 1) + float(value)) / length
        result[index] = current_adx
    return result


def evaluate_market_signal(
    candles: list[Candle],
    index: int,
    strategy: dict[str, Any],
    fast: list[float | None],
    slow: list[float | None],
    atrs: list[float | None],
    vol_sma: list[float | None],
    adxs: list[float | None],
) -> tuple[str | None, str, str]:
    candle = candles[index]
    atr_value = atrs[index]
    if atr_value is None or fast[index] is None or slow[index] is None or vol_sma[index] is None:
        return None, "", "indicators warming up"
    if adxs[index] is None:
        return None, "", "ADX warming up"
    if float(adxs[index]) < float(strategy.get("min_adx", 0)):
        return None, "", f"ADX filter: {float(adxs[index]):.1f}"

    atr_percent = (atr_value / candle.close) * 100
    if atr_percent < float(strategy["min_atr_percent"]) or atr_percent > float(strategy["max_atr_percent"]):
        return None, "", f"ATR filter: {atr_percent:.2f}%"

    lookback = int(strategy["breakout_lookback"])
    previous = candles[index - lookback : index]
    if len(previous) < lookback:
        return None, "", "breakout history warming up"
    previous_high = max(item.high for item in previous)
    previous_low = min(item.low for item in previous)
    volume_ok = candle.volume >= float(vol_sma[index]) * float(strategy["min_volume_factor"])
    ema_gap_percent = abs(float(fast[index]) - float(slow[index])) / candle.close * 100
    min_gap = float(strategy.get("min_ema_gap_percent", 0))
    slope_lookback = int(strategy.get("slow_slope_lookback", 1))
    if index - slope_lookback < 0 or slow[index - slope_lookback] is None:
        return None, "", "trend slope warming up"
    slow_slope_percent = (
        float(slow[index]) - float(slow[index - slope_lookback])
    ) / candle.close * 100
    min_slope = float(strategy.get("min_slow_slope_percent", 0))
    uptrend = (
        float(fast[index]) > float(slow[index])
        and candle.close > float(slow[index])
        and ema_gap_percent >= min_gap
        and slow_slope_percent >= min_slope
    )
    downtrend = (
        float(fast[index]) < float(slow[index])
        and candle.close < float(slow[index])
        and ema_gap_percent >= min_gap
        and slow_slope_percent <= -min_slope
    )

    if uptrend and candle.close > previous_high:
        if not volume_ok:
            return None, "", "volume filter on bullish breakout"
        return "long", f"EMA trend + {lookback}-bar high breakout", "long signal"
    if bool(strategy.get("allow_shorts", True)) and downtrend and candle.close < previous_low:
        if not volume_ok:
            return None, "", "volume filter on bearish breakout"
        return "short", f"EMA trend + {lookback}-bar low breakout", "short signal"
    return None, "", "no signal"


def position_unrealized(position: dict[str, Any], price: float) -> float:
    qty = float(position["qty"])
    entry = float(position["entry"])
    if position["side"] == "long":
        return (price - entry) * qty
    return (entry - price) * qty


def can_trade(state: dict[str, Any], ctx: BotContext) -> tuple[bool, str]:
    account = ctx.config["account"]
    if ctx.mode != "paper":
        if not ctx.orders_enabled:
            return False, "Binance orders disabled"
        if not state.get("broker_status", {}).get("connected"):
            return False, "Binance disconnected"

    daily = daily_stats(state, ctx)
    if ctx.mode != "paper" and float(state.get("balance", 0.0)) <= 0:
        return False, "Demo balance is zero; add virtual USDT first"
    if int(daily["trades"]) >= int(account["max_daily_trades"]):
        return False, "daily trade limit reached"

    initial = float(state.get("initial_balance", account["initial_balance"]))
    max_loss = initial * float(account["max_daily_loss_percent"]) / 100.0
    if float(daily.get("realized_pnl", 0.0)) <= -max_loss:
        return False, "daily loss limit reached"

    if len(state.get("positions", {})) >= int(account["max_open_positions"]):
        return False, "max open positions reached"

    return True, "ok"


def calc_qty(balance: float, entry: float, stop: float, risk_percent: float) -> float:
    risk_cash = balance * risk_percent / 100.0
    risk_per_unit = abs(entry - stop)
    if risk_cash <= 0 or risk_per_unit <= 0:
        return 0.0
    return risk_cash / risk_per_unit


def close_position(
    ctx: BotContext,
    state: dict[str, Any],
    symbol: str,
    price: float,
    reason: str,
) -> None:
    position = state.setdefault("positions", {}).pop(symbol, None)
    if not position:
        return

    pnl = position_unrealized(position, price)
    state["balance"] = float(state["balance"]) + pnl
    state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + pnl
    daily = daily_stats(state, ctx)
    daily["realized_pnl"] = float(daily.get("realized_pnl", 0.0)) + pnl

    row = {
        "time": now_iso(ctx.timezone),
        "event": "close",
        "symbol": symbol,
        "side": position["side"],
        "qty": round(float(position["qty"]), 8),
        "price": round(price, 8),
        "stop": round(float(position["stop"]), 8),
        "target": round(float(position["target"]), 8),
        "pnl": round(pnl, 2),
        "balance": round(float(state["balance"]), 2),
        "reason": reason,
    }
    append_trade(ctx, state, row)
    log_event(state, f"{symbol}: closed {position['side']} at {price:.6g}, pnl={pnl:.2f} ({reason})", ctx.timezone)
    notify_safely(
        ctx,
        state,
        f"Crypto Autobot [{ctx.mode.upper()}]\n"
        f"Closed {position['side']} {symbol}\nPnL: {pnl:.2f} USDT\nReason: {reason}",
    )


def open_position(
    ctx: BotContext,
    state: dict[str, Any],
    symbol: str,
    side: str,
    candle: Candle,
    atr_value: float,
    reason: str,
) -> None:
    account = ctx.config["account"]
    strategy = ctx.config["strategy"]
    entry = candle.close

    if side == "long":
        stop = entry - atr_value * float(strategy["stop_atr"])
        target = entry + atr_value * float(strategy["target_atr"])
    else:
        stop = entry + atr_value * float(strategy["stop_atr"])
        target = entry - atr_value * float(strategy["target_atr"])

    broker_result: dict[str, Any] | None = None
    if ctx.broker is not None:
        broker_result = ctx.broker.open_position(
            symbol=symbol,
            side=side,
            stop_distance=abs(entry - stop),
            target_distance=abs(target - entry),
            risk_percent=float(account["risk_per_trade_percent"]),
            max_open_positions=int(account["max_open_positions"]),
        )
        entry = float(broker_result["entry"])
        stop = float(broker_result["stop"])
        target = float(broker_result["target"])
        qty = float(broker_result["quantity"])
        state["balance"] = float(broker_result["wallet_balance"])
    else:
        qty = calc_qty(float(state["balance"]), entry, stop, float(account["risk_per_trade_percent"]))
        if qty <= 0 or not math.isfinite(qty):
            log_event(state, f"{symbol}: blocked, invalid position size", ctx.timezone)
            return

    position = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "qty": qty,
        "stop": stop,
        "initial_stop": stop,
        "target": target,
        "opened_at": now_iso(ctx.timezone),
        "opened_candle_time": candle.open_time,
        "reason": reason,
        "highest": candle.high,
        "lowest": candle.low,
        "mode": ctx.mode,
    }
    if broker_result:
        position.update(
            {
                "entry_order_id": broker_result.get("entry_order_id"),
                "stop_algo_id": broker_result.get("stop_algo_id"),
                "target_algo_id": broker_result.get("target_algo_id"),
                "opened_at_ms": broker_result.get("opened_at_ms"),
            }
        )
    state.setdefault("positions", {})[symbol] = position
    daily = daily_stats(state, ctx)
    daily["trades"] = int(daily.get("trades", 0)) + 1

    row = {
        "time": now_iso(ctx.timezone),
        "event": "open",
        "symbol": symbol,
        "side": side,
        "qty": round(qty, 8),
        "price": round(entry, 8),
        "stop": round(stop, 8),
        "target": round(target, 8),
        "pnl": 0,
        "balance": round(float(state["balance"]), 2),
        "reason": reason,
    }
    append_trade(ctx, state, row)
    destination = "Binance" if ctx.broker else "paper"
    log_event(
        state,
        f"{symbol}: opened {side} on {destination} at {entry:.6g}, "
        f"stop={stop:.6g}, target={target:.6g}",
        ctx.timezone,
    )
    notify_safely(
        ctx,
        state,
        f"Crypto Autobot [{ctx.mode.upper()}]\n"
        f"Opened {side} {symbol}\nEntry: {entry:.6g}\n"
        f"Stop: {stop:.6g}\nTarget: {target:.6g}",
    )


def refresh_exchange_account(ctx: BotContext, state: dict[str, Any]) -> None:
    if ctx.broker is None:
        state["broker_status"] = {
            "mode": "paper",
            "connected": True,
            "orders_enabled": True,
            "message": "Paper simulator",
        }
        return

    try:
        summary = ctx.broker.account_summary()
        positions = {
            str(item["symbol"]): item
            for item in summary["positions"]
            if str(item.get("symbol", "")).upper()
        }
        ctx.exchange_snapshot = positions
        wallet_balance = float(summary["balance"])
        state["balance"] = wallet_balance
        if wallet_balance > 0 and (
            not state.get("exchange_balance_initialized")
            or float(state.get("initial_balance", 0.0)) <= 0
        ):
            state["initial_balance"] = wallet_balance
            state["exchange_balance_initialized"] = True
        state["exchange_positions"] = positions
        state["broker_status"] = {
            "mode": ctx.mode,
            "connected": True,
            "orders_enabled": ctx.orders_enabled,
            "environment": summary["environment"],
            "position_mode": summary["position_mode"],
            "available_balance": float(summary["available_balance"]),
            "message": "Connected",
        }
        state.pop("last_broker_error", None)
    except Exception as exc:
        message = str(exc)
        state["broker_status"] = {
            "mode": ctx.mode,
            "connected": False,
            "orders_enabled": ctx.orders_enabled,
            "message": message,
        }
        if state.get("last_broker_error") != message:
            log_event(state, f"Binance connection error: {message}", ctx.timezone)
            state["last_broker_error"] = message
        ctx.exchange_snapshot = {}


def record_exchange_close(
    ctx: BotContext,
    state: dict[str, Any],
    symbol: str,
    position: dict[str, Any],
    price: float,
) -> None:
    if ctx.broker is None:
        return
    opened_at_ms = int(position.get("opened_at_ms") or int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000)
    pnl_info = ctx.broker.realized_pnl_since(symbol, opened_at_ms)
    pnl = float(pnl_info["net_pnl"])
    state.setdefault("positions", {}).pop(symbol, None)
    state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + pnl
    daily = daily_stats(state, ctx)
    daily["realized_pnl"] = float(daily.get("realized_pnl", 0.0)) + pnl
    balance = ctx.broker.get_balance()
    state["balance"] = float(balance["balance"])

    row = {
        "time": now_iso(ctx.timezone),
        "event": "close",
        "symbol": symbol,
        "side": position["side"],
        "qty": round(float(position["qty"]), 8),
        "price": round(price, 8),
        "stop": round(float(position["stop"]), 8),
        "target": round(float(position["target"]), 8),
        "pnl": round(pnl, 2),
        "balance": round(float(state["balance"]), 2),
        "reason": (
            f"Binance position closed; realized={pnl_info['realized_pnl']:.2f}, "
            f"commission={pnl_info['commission']:.2f}"
        ),
    }
    append_trade(ctx, state, row)
    ctx.broker.cancel_protection(symbol)
    log_event(state, f"{symbol}: Binance position closed, net pnl={pnl:.2f}", ctx.timezone)
    notify_safely(
        ctx,
        state,
        f"Crypto Autobot [{ctx.mode.upper()}]\nClosed {position['side']} {symbol}\n"
        f"Net PnL: {pnl:.2f} USDT",
    )


def sync_exchange_position(
    ctx: BotContext,
    state: dict[str, Any],
    symbol: str,
    candle: Candle,
) -> None:
    position = state.get("positions", {}).get(symbol)
    if not position or ctx.broker is None:
        return
    if not state.get("broker_status", {}).get("connected"):
        return
    exchange_position = ctx.exchange_snapshot.get(symbol)
    if exchange_position:
        amount = float(exchange_position.get("positionAmt", 0))
        position["entry"] = float(exchange_position.get("entryPrice") or position["entry"])
        position["qty"] = abs(amount)
        position["side"] = "long" if amount > 0 else "short"
        position["exchange_unrealized_pnl"] = float(exchange_position.get("unRealizedProfit", 0))
        if ctx.orders_enabled and not position.get("emergency_close_sent"):
            if not ctx.broker.has_stop_and_target(symbol):
                ctx.broker.cancel_protection(symbol)
                ctx.broker.market_close(symbol, exchange_position)
                position["emergency_close_sent"] = True
                log_event(
                    state,
                    f"{symbol}: protection missing; emergency Binance close sent",
                    ctx.timezone,
                )
                notify_safely(
                    ctx,
                    state,
                    f"Crypto Autobot [{ctx.mode.upper()}]\n"
                    f"Emergency close sent for {symbol}: Stop Loss or Take Profit was missing.",
                )
        return
    record_exchange_close(ctx, state, symbol, position, candle.close)


def manage_position(ctx: BotContext, state: dict[str, Any], symbol: str, candle: Candle, atr_value: float | None) -> None:
    position = state.get("positions", {}).get(symbol)
    if not position:
        return
    if ctx.broker is not None:
        sync_exchange_position(ctx, state, symbol, candle)
        return

    side = position["side"]
    stop = float(position["stop"])
    target = float(position["target"])
    entry = float(position["entry"])
    same_policy = str(ctx.config["account"].get("same_candle_exit", "stop_first"))

    if atr_value:
        if side == "long":
            position["highest"] = max(float(position.get("highest", candle.high)), candle.high)
            risk = entry - float(position["initial_stop"])
            if risk > 0 and candle.high >= entry + risk * float(ctx.config["strategy"]["trail_after_r"]):
                position["stop"] = max(stop, candle.close - atr_value * float(ctx.config["strategy"]["trail_atr"]))
                stop = float(position["stop"])
        else:
            position["lowest"] = min(float(position.get("lowest", candle.low)), candle.low)
            risk = float(position["initial_stop"]) - entry
            if risk > 0 and candle.low <= entry - risk * float(ctx.config["strategy"]["trail_after_r"]):
                position["stop"] = min(stop, candle.close + atr_value * float(ctx.config["strategy"]["trail_atr"]))
                stop = float(position["stop"])

    if side == "long":
        hit_stop = candle.low <= stop
        hit_target = candle.high >= target
    else:
        hit_stop = candle.high >= stop
        hit_target = candle.low <= target

    if hit_stop and hit_target:
        if same_policy == "target_first":
            close_position(ctx, state, symbol, target, "target and stop touched, target_first")
        else:
            close_position(ctx, state, symbol, stop, "target and stop touched, stop_first")
    elif hit_stop:
        close_position(ctx, state, symbol, stop, "stop")
    elif hit_target:
        close_position(ctx, state, symbol, target, "target")


def scan_symbol(ctx: BotContext, state: dict[str, Any], symbol: str) -> dict[str, Any]:
    market = ctx.config["market"]
    strategy = ctx.config["strategy"]
    candles = fetch_klines(
        str(market["base_url"]),
        symbol,
        str(market["interval"]),
        int(market["history_limit"]),
    )
    min_needed = max(
        int(strategy["slow_ema"]) + int(strategy.get("slow_slope_lookback", 1)),
        int(strategy["breakout_lookback"]) + 2,
        int(strategy["volume_sma_length"]),
        int(strategy["atr_length"]),
    )
    if len(candles) < min_needed:
        return {"symbol": symbol, "status": "not enough candles", "candles": len(candles)}

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    fast = ema(closes, int(strategy["fast_ema"]))
    slow = ema(closes, int(strategy["slow_ema"]))
    atrs = atr(candles, int(strategy["atr_length"]))
    vol_sma = sma(volumes, int(strategy["volume_sma_length"]))
    adxs = adx(candles, int(strategy.get("adx_length", 14)))

    candle = candles[-1]
    i = len(candles) - 1
    atr_value = atrs[i]
    manage_position(ctx, state, symbol, candle, atr_value)

    latest = {
        "symbol": symbol,
        "time": candle.open_dt,
        "price": candle.close,
        "fast_ema": fast[i],
        "slow_ema": slow[i],
        "atr": atr_value,
        "status": "no signal",
    }

    if not strategy.get("enabled", True):
        latest["status"] = "strategy disabled"
        return latest

    if symbol in state.get("positions", {}):
        latest["status"] = "position open"
        return latest
    if symbol in state.get("exchange_positions", {}):
        latest["status"] = "external Binance position; bot will not modify it"
        return latest

    side, reason, signal_status = evaluate_market_signal(
        candles,
        i,
        strategy,
        fast,
        slow,
        atrs,
        vol_sma,
        adxs,
    )
    if not side:
        latest["status"] = signal_status
        return latest

    can_open, reason_block = can_trade(state, ctx)
    if not can_open:
        latest["status"] = reason_block
        return latest

    seen_key = f"{symbol}:{market['interval']}:{candle.open_time}"
    if state.setdefault("seen_signal_candles", {}).get(symbol) == seen_key:
        latest["status"] = "latest candle already processed"
        return latest

    if atr_value is None:
        latest["status"] = "ATR unavailable"
        return latest
    open_position(ctx, state, symbol, side, candle, atr_value, reason)
    state["seen_signal_candles"][symbol] = seen_key
    latest["status"] = f"opened {side}"

    return latest


def scan_once(ctx: BotContext) -> dict[str, Any]:
    with ctx.lock:
        ensure_trades_file(ctx)
        state = ensure_state(ctx)
        refresh_exchange_account(ctx, state)
        results: list[dict[str, Any]] = []
        for symbol in ctx.config["market"]["symbols"]:
            try:
                result = scan_symbol(ctx, state, str(symbol).upper())
                results.append(result)
                state.setdefault("latest", {})[str(symbol).upper()] = result
            except Exception as exc:  # noqa: BLE001
                message = f"{symbol}: scan error: {exc}"
                results.append({"symbol": symbol, "status": message})
                log_event(state, message, ctx.timezone)
        write_state(ctx, state)
        return {"status": "ok", "results": results, "state": public_state(ctx, state)}


def open_demo_test_order(
    ctx: BotContext,
    symbol: str,
    side: str,
    confirmation: str,
) -> dict[str, Any]:
    if ctx.mode != "demo":
        raise ValueError("Test order is available only in Binance Demo mode.")
    if confirmation != DEMO_TEST_CONFIRMATION:
        raise ValueError("Demo test order was not confirmed.")
    if not ctx.orders_enabled or ctx.broker is None:
        raise ValueError("Binance Demo orders are disabled.")

    symbol = symbol.upper()
    side = side.lower()
    allowed_symbols = [str(item).upper() for item in ctx.config["market"]["symbols"]]
    if symbol not in allowed_symbols:
        raise ValueError("Choose a symbol from the active Demo profile.")
    if side not in ("long", "short"):
        raise ValueError("Side must be long or short.")

    with ctx.lock:
        ensure_trades_file(ctx)
        state = ensure_state(ctx)
        refresh_exchange_account(ctx, state)
        if not state.get("broker_status", {}).get("connected"):
            raise ValueError("Binance Demo is not connected.")
        if float(state.get("broker_status", {}).get("available_balance", 0.0)) <= 0:
            raise ValueError("Demo balance is zero. Add virtual USDT in Binance Demo first.")
        if symbol in state.get("positions", {}) or symbol in state.get("exchange_positions", {}):
            raise ValueError(f"{symbol} already has an open position.")

        can_open, blocked_reason = can_trade(state, ctx)
        if not can_open:
            raise ValueError(blocked_reason)

        market = ctx.config["market"]
        candles = fetch_klines(
            str(market["base_url"]),
            symbol,
            str(market["interval"]),
            int(market["history_limit"]),
        )
        atr_values = atr(candles, int(ctx.config["strategy"]["atr_length"]))
        if not candles or not atr_values or atr_values[-1] is None:
            raise ValueError("Not enough closed candles to calculate a protected test order.")

        candle = candles[-1]
        open_position(
            ctx,
            state,
            symbol,
            side,
            candle,
            float(atr_values[-1]),
            "manual Binance Demo market test",
        )
        state.setdefault("latest", {})[symbol] = {
            "symbol": symbol,
            "time": candle.open_dt,
            "price": candle.close,
            "status": f"manual Demo test opened {side}",
        }
        write_state(ctx, state)
        return {"status": "ok", "state": public_state(ctx, state)}


def worker_loop(controller: RuntimeController) -> None:
    while not controller.stop_event.is_set():
        ctx = controller.current()
        try:
            scan_once(ctx)
        except Exception as exc:  # noqa: BLE001
            with ctx.lock:
                state = ensure_state(ctx)
                log_event(state, f"worker error: {exc}", ctx.timezone)
                write_state(ctx, state)
        interval = int(ctx.config["app"]["scan_interval_seconds"])
        controller.wake_event.wait(interval)
        controller.wake_event.clear()


def stats_from_state(state: dict[str, Any]) -> dict[str, Any]:
    trades = [t for t in state.get("trades", []) if t.get("event") == "close"]
    wins = [t for t in trades if float(t.get("pnl", 0)) > 0]
    losses = [t for t in trades if float(t.get("pnl", 0)) < 0]
    pnl = float(state.get("realized_pnl", 0.0))
    initial = float(state.get("initial_balance", 0.0))
    return {
        "balance": round(float(state.get("balance", 0.0)), 2),
        "initial_balance": round(initial, 2),
        "realized_pnl": round(pnl, 2),
        "return_percent": round((pnl / initial) * 100, 2) if initial else 0,
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "open_positions": len(state.get("positions", {})),
    }


def public_state(ctx: BotContext, state: dict[str, Any]) -> dict[str, Any]:
    prices = {symbol: data.get("price") for symbol, data in state.get("latest", {}).items()}
    open_pnl = 0.0
    positions = {}
    for symbol, position in state.get("positions", {}).items():
        price = prices.get(symbol) or position.get("entry")
        if ctx.broker is not None and position.get("exchange_unrealized_pnl") is not None:
            unrealized = float(position["exchange_unrealized_pnl"])
        else:
            unrealized = position_unrealized(position, float(price))
        open_pnl += unrealized
        item = dict(position)
        item["unrealized_pnl"] = round(unrealized, 2)
        positions[symbol] = item

    displayed_positions = dict(positions)
    for symbol, exchange_position in state.get("exchange_positions", {}).items():
        if symbol in displayed_positions:
            continue
        amount = float(exchange_position.get("positionAmt", 0))
        displayed_positions[symbol] = {
            "symbol": symbol,
            "side": "long" if amount > 0 else "short",
            "entry": float(exchange_position.get("entryPrice", 0)),
            "qty": abs(amount),
            "stop": None,
            "target": None,
            "unrealized_pnl": round(float(exchange_position.get("unRealizedProfit", 0)), 2),
            "external": True,
        }
        open_pnl += float(exchange_position.get("unRealizedProfit", 0))

    stats = stats_from_state(state)
    stats["open_positions"] = len(displayed_positions)
    result = {
        "updated_at": state.get("updated_at"),
        "mode": ctx.mode,
        "orders_enabled": ctx.orders_enabled,
        "market": {
            "symbols": list(ctx.config.get("market", {}).get("symbols", [])),
            "interval": ctx.config.get("market", {}).get("interval"),
        },
        "broker_status": state.get("broker_status", {}),
        "stats": stats,
        "equity_now": round(float(state.get("balance", 0.0)) + open_pnl, 2),
        "positions": displayed_positions,
        "latest": state.get("latest", {}),
        "trades": list(reversed(state.get("trades", [])[-80:])),
        "logs": list(
            reversed(
                [
                    item
                    for item in state.get("logs", [])
                    if str(item.get("message", "")).strip().lower() != "scan started"
                ][-80:]
            )
        ),
    }
    result["stats"]["open_unrealized_pnl"] = round(open_pnl, 2)
    return result


def send_json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def send_html(handler: BaseHTTPRequestHandler, html: str) -> None:
    raw = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def dashboard_html(app_name: str) -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{app_name}</title>
  <style>
    :root {{
      --bg: #f3f5f7;
      --panel: #ffffff;
      --text: #17202a;
      --muted: #66727f;
      --green: #087f5b;
      --red: #c92a2a;
      --amber: #b36b00;
      --blue: #1c5fd4;
      --line: #dfe4e8;
    }}
    * {{ box-sizing: border-box; }}
    [hidden] {{ display: none !important; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      padding: 18px clamp(16px, 4vw, 42px);
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{ margin: 0; font-size: 21px; }}
    .muted {{ color: var(--muted); }}
    header > div {{ min-width: 0; }}
    .statusline {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 7px; }}
    .badge {{
      display: inline-flex;
      flex: 0 0 auto;
      align-items: center;
      min-height: 26px;
      padding: 4px 9px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f8fafb;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .badge.ok {{ color: var(--green); border-color: #9bd5c2; background: #effaf6; }}
    .badge.warn {{ color: var(--amber); border-color: #edcc91; background: #fff8e8; }}
    .badge.danger {{ color: var(--red); border-color: #efb0b0; background: #fff1f1; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(160px, 1fr));
      gap: 12px;
      padding: 18px clamp(16px, 4vw, 42px);
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 15px;
      min-height: 90px;
    }}
    .label {{ color: var(--muted); font-size: 13px; }}
    .value {{ font-size: 26px; font-weight: 750; margin-top: 7px; }}
    .green {{ color: var(--green); }}
    .red {{ color: var(--red); }}
    main {{
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.65fr);
      gap: 14px;
      padding: 0 clamp(16px, 4vw, 42px) 32px;
    }}
    section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
    }}
    section h2 {{
      margin: 0;
      padding: 14px 16px;
      font-size: 15px;
      background: #f8fafb;
      border-bottom: 1px solid var(--line);
    }}
    .tablewrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      text-align: left;
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 650; }}
    main > *, .stack, section {{ min-width: 0; }}
    .stack {{ display: grid; gap: 14px; align-content: start; }}
    .logs {{ max-height: 420px; overflow: auto; }}
    .log {{ padding: 10px 12px; border-bottom: 1px solid var(--line); font-size: 13px; }}
    button {{
      background: var(--blue);
      color: white;
      border: 0;
      border-radius: 6px;
      padding: 10px 14px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:disabled {{ opacity: .55; cursor: progress; }}
    .controlbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin: 18px clamp(16px, 4vw, 42px) 0;
      padding: 14px 16px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
    }}
    .controlcopy {{ min-width: 190px; }}
    .controlcopy strong {{ display: block; font-size: 14px; }}
    .controlcopy span {{ display: block; margin-top: 4px; font-size: 12px; }}
    .modecontrols {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }}
    .modebtn {{ background: #e9eef5; color: #354052; border: 1px solid #ced6df; }}
    .modebtn.active {{ background: var(--blue); color: white; border-color: var(--blue); }}
    .modebtn.unavailable {{ border-style: dashed; }}
    .modebtn.live {{ color: var(--red); }}
    .modebtn.live.active {{ background: var(--red); color: white; border-color: var(--red); }}
    .testcontrols {{ display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }}
    .testcontrols select {{ min-height: 38px; padding: 8px 10px; border: 1px solid #cbd3dc; border-radius: 6px; background: white; }}
    .longbtn {{ background: var(--green); }}
    .shortbtn {{ background: var(--red); }}
    input {{
      min-height: 38px;
      border: 1px solid #cbd3dc;
      border-radius: 6px;
      padding: 8px 10px;
      background: white;
      color: var(--text);
      font: inherit;
    }}
    #controlToken {{ width: 170px; }}
    dialog {{
      width: min(440px, calc(100vw - 32px));
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0;
      color: var(--text);
      box-shadow: 0 18px 60px rgba(24, 34, 46, .24);
    }}
    dialog::backdrop {{ background: rgba(23, 32, 42, .45); }}
    .dialogbody {{ padding: 20px; }}
    .dialogbody h2 {{ margin: 0 0 8px; font-size: 19px; }}
    .dialogbody p {{ margin: 0 0 14px; line-height: 1.45; }}
    .dialogbody input {{ width: 100%; }}
    .dialogactions {{ display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }}
    .secondary {{ background: #e9eef5; color: #354052; }}
    .dangerbtn {{ background: var(--red); }}
    @media (max-width: 920px) {{
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      main {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 560px) {{
      header {{ align-items: flex-start; flex-direction: column; }}
      header > div {{ width: 100%; }}
      #updated {{ overflow-wrap: anywhere; }}
      .grid {{ grid-template-columns: 1fr; }}
      th, td {{ font-size: 12px; padding: 9px 8px; }}
      .controlbar {{ align-items: stretch; flex-direction: column; }}
      .modecontrols {{ display: grid; grid-template-columns: repeat(3, 1fr); }}
      .modecontrols button {{ padding-inline: 8px; }}
      #controlToken {{ width: 100%; grid-column: 1 / -1; }}
      .testcontrols {{ display: grid; grid-template-columns: 1fr 1fr; }}
      .testcontrols select {{ grid-column: 1 / -1; width: 100%; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>{app_name}</h1>
      <div class="statusline">
        <span class="badge" id="modeBadge">Режим: -</span>
        <span class="badge" id="brokerBadge">Binance: -</span>
        <span class="badge" id="ordersBadge">Ордера: -</span>
      </div>
      <div class="muted" id="updated">Загрузка...</div>
    </div>
    <button id="scanBtn">Проверить сейчас</button>
  </header>

  <div class="controlbar">
    <div class="controlcopy">
      <strong>Торговый режим</strong>
      <span class="muted" id="modeHint">Загрузка доступных режимов...</span>
    </div>
    <div class="modecontrols">
      <button class="modebtn" data-mode="paper">Paper</button>
      <button class="modebtn" data-mode="demo">Demo</button>
      <button class="modebtn live" data-mode="live">Live</button>
      <input id="controlToken" type="password" autocomplete="current-password" placeholder="Код управления" hidden>
    </div>
  </div>

  <div class="controlbar" id="demoTestBar" hidden>
    <div class="controlcopy">
      <strong>Проверка рыночного ордера</strong>
      <span class="muted" id="demoTestHint">Только Binance Demo. Бот сразу добавит стоп и тейк.</span>
    </div>
    <div class="testcontrols">
      <select id="demoTestSymbol" aria-label="Торговая пара"></select>
      <button class="longbtn" data-test-side="long">Test Long</button>
      <button class="shortbtn" data-test-side="short">Test Short</button>
    </div>
  </div>

  <div class="grid">
    <div class="card"><div class="label">Средства с открытым PnL</div><div class="value" id="equity">-</div></div>
    <div class="card"><div class="label">Закрытый PnL</div><div class="value" id="pnl">-</div></div>
    <div class="card"><div class="label">Процент прибыльных</div><div class="value" id="winrate">-</div></div>
    <div class="card"><div class="label">Открытые позиции</div><div class="value" id="openpos">-</div></div>
  </div>

  <main>
    <div class="stack">
      <section>
        <h2>Состояние рынка</h2>
        <div class="tablewrap"><table>
          <thead><tr><th>Пара</th><th>Цена</th><th>Свеча</th><th>Решение бота</th></tr></thead>
          <tbody id="latestRows"></tbody>
        </table></div>
      </section>
      <section>
        <h2>Позиции</h2>
        <div class="tablewrap"><table>
          <thead><tr><th>Пара</th><th>Сторона</th><th>Вход</th><th>Стоп</th><th>Тейк</th><th>Открытый PnL</th></tr></thead>
          <tbody id="positionRows"></tbody>
        </table></div>
      </section>
      <section>
        <h2>Журнал сделок</h2>
        <div class="tablewrap"><table>
          <thead><tr><th>Время</th><th>Событие</th><th>Пара</th><th>Сторона</th><th>Цена</th><th>PnL</th><th>Причина</th></tr></thead>
          <tbody id="tradeRows"></tbody>
        </table></div>
      </section>
    </div>

    <section>
      <h2>События</h2>
      <div class="logs" id="logs"></div>
    </section>
  </main>

  <dialog id="liveDialog">
    <div class="dialogbody">
      <h2>Включение Live</h2>
      <p class="muted">Этот режим работает с реальным балансом Binance. Для подтверждения введи фразу:</p>
      <p><strong>{LIVE_CONFIRMATION}</strong></p>
      <input id="liveConfirmation" autocomplete="off" placeholder="Фраза подтверждения">
      <div class="dialogactions">
        <button class="secondary" id="cancelLiveBtn">Отмена</button>
        <button class="dangerbtn" id="confirmLiveBtn">Переключить на Live</button>
      </div>
    </div>
  </dialog>

<script>
const money = v => Number(v || 0).toLocaleString(undefined, {{maximumFractionDigits: 2}});
const num = v => v === null || v === undefined ? '-' : Number(v).toLocaleString(undefined, {{maximumFractionDigits: 6}});
const cls = v => Number(v || 0) >= 0 ? 'green' : 'red';
const esc = v => String(v ?? '-').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
let currentMode = null;
let switchingMode = false;
let modeOptions = {{}};

function emptyRow(cols, text) {{
  return `<tr><td colspan="${{cols}}" class="muted">${{esc(text)}}</td></tr>`;
}}

async function loadState() {{
  const res = await fetch('/api/state');
  const data = await res.json();
  currentMode = data.mode;
  document.getElementById('updated').textContent = `Обновлено: ${{data.updated_at || '-'}}`;
  const connected = Boolean(data.broker_status?.connected);
  document.getElementById('modeBadge').textContent = `Режим: ${{String(data.mode || '-').toUpperCase()}}`;
  document.getElementById('modeBadge').className = `badge ${{data.mode === 'live' ? 'danger' : (data.mode === 'demo' ? 'warn' : 'ok')}}`;
  document.getElementById('brokerBadge').textContent = `Binance: ${{connected ? 'подключён' : 'нет подключения'}}`;
  document.getElementById('brokerBadge').className = `badge ${{connected ? 'ok' : 'danger'}}`;
  document.getElementById('ordersBadge').textContent = `Ордера: ${{data.orders_enabled ? 'разрешены' : 'заблокированы'}}`;
  document.getElementById('ordersBadge').className = `badge ${{data.orders_enabled ? (data.mode === 'live' ? 'danger' : 'warn') : 'ok'}}`;
  const demoTestBar = document.getElementById('demoTestBar');
  demoTestBar.hidden = !(data.mode === 'demo' && connected && data.orders_enabled);
  const symbolSelect = document.getElementById('demoTestSymbol');
  const symbols = data.market?.symbols || [];
  const selectedSymbol = symbolSelect.value;
  symbolSelect.innerHTML = symbols.map(symbol => `<option value="${{esc(symbol)}}">${{esc(symbol)}}</option>`).join('');
  if (symbols.includes(selectedSymbol)) symbolSelect.value = selectedSymbol;
  document.getElementById('equity').textContent = `$${{money(data.equity_now)}}`;
  document.getElementById('pnl').textContent = `$${{money(data.stats.realized_pnl)}}`;
  document.getElementById('pnl').className = `value ${{cls(data.stats.realized_pnl)}}`;
  document.getElementById('winrate').textContent = `${{money(data.stats.win_rate)}}%`;
  document.getElementById('openpos').textContent = data.stats.open_positions;

  const latest = Object.values(data.latest || {{}});
  document.getElementById('latestRows').innerHTML = latest.length
    ? latest.map(x => `<tr><td>${{esc(x.symbol)}}</td><td>${{num(x.price)}}</td><td>${{esc(x.time)}}</td><td>${{esc(x.status)}}</td></tr>`).join('')
    : emptyRow(4, 'Проверок ещё не было');

  const positions = Object.values(data.positions || {{}});
  document.getElementById('positionRows').innerHTML = positions.length
    ? positions.map(p => `<tr><td>${{esc(p.symbol)}}${{p.external ? ' *' : ''}}</td><td>${{esc(p.side)}}</td><td>${{num(p.entry)}}</td><td>${{num(p.stop)}}</td><td>${{num(p.target)}}</td><td class="${{cls(p.unrealized_pnl)}}">$${{money(p.unrealized_pnl)}}</td></tr>`).join('')
    : emptyRow(6, 'Открытых позиций нет');

  document.getElementById('tradeRows').innerHTML = data.trades.length
    ? data.trades.map(t => `<tr><td>${{esc(t.time)}}</td><td>${{esc(t.event)}}</td><td>${{esc(t.symbol)}}</td><td>${{esc(t.side)}}</td><td>${{num(t.price)}}</td><td class="${{cls(t.pnl)}}">$${{money(t.pnl)}}</td><td>${{esc(t.reason)}}</td></tr>`).join('')
    : emptyRow(7, 'Сделок ещё нет');

  document.getElementById('logs').innerHTML = data.logs.length
    ? data.logs.map(l => `<div class="log"><span class="muted">${{esc(l.time)}}</span><br>${{esc(l.message)}}</div>`).join('')
    : '<div class="log muted">Событий пока нет</div>';

  const control = data.mode_control || {{}};
  const options = control.options || {{}};
  modeOptions = options;
  const tokenInput = document.getElementById('controlToken');
  tokenInput.hidden = !control.requires_token;
  document.querySelectorAll('.modebtn').forEach(btn => {{
    const mode = btn.dataset.mode;
    const option = options[mode] || {{}};
    btn.classList.toggle('active', mode === data.mode);
    btn.classList.toggle('unavailable', !option.available);
    btn.disabled = switchingMode || mode === data.mode || !control.control_available;
    btn.title = option.reason || '';
  }});
  const currentOption = options[data.mode] || {{}};
  document.getElementById('modeHint').textContent = control.control_available
    ? (currentOption.summary || 'Выбери режим работы')
    : (control.control_reason || 'Переключение недоступно');
}}

async function switchMode(mode, confirmation = '') {{
  if (switchingMode || mode === currentMode) return;
  switchingMode = true;
  document.getElementById('modeHint').textContent = `Переключаю на ${{mode.toUpperCase()}}...`;
  try {{
    const res = await fetch('/api/mode', {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/json',
        'X-Control-Token': document.getElementById('controlToken').value
      }},
      body: JSON.stringify({{mode, confirmation}})
    }});
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || 'Не удалось переключить режим');
    await loadState();
  }} catch (error) {{
    document.getElementById('modeHint').textContent = error.message;
  }} finally {{
    switchingMode = false;
    await loadState();
  }}
}}

document.querySelectorAll('.modebtn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const mode = btn.dataset.mode;
    const option = modeOptions[mode] || {{}};
    if (!option.available) {{
      document.getElementById('modeHint').textContent = option.reason || 'Этот режим пока недоступен.';
      return;
    }}
    if (mode === 'live') {{
      document.getElementById('liveConfirmation').value = '';
      document.getElementById('liveDialog').showModal();
      return;
    }}
    switchMode(mode);
  }});
}});

document.getElementById('cancelLiveBtn').addEventListener('click', () => {{
  document.getElementById('liveDialog').close();
}});
document.getElementById('confirmLiveBtn').addEventListener('click', () => {{
  const confirmation = document.getElementById('liveConfirmation').value;
  document.getElementById('liveDialog').close();
  switchMode('live', confirmation);
}});

document.getElementById('scanBtn').addEventListener('click', async () => {{
  const btn = document.getElementById('scanBtn');
  btn.disabled = true;
  btn.textContent = 'Проверяю...';
  try {{
    await fetch('/api/scan', {{method: 'POST'}});
    await loadState();
  }} finally {{
    btn.disabled = false;
    btn.textContent = 'Проверить сейчас';
  }}
}});

document.querySelectorAll('[data-test-side]').forEach(btn => {{
  btn.addEventListener('click', async () => {{
    const side = btn.dataset.testSide;
    const symbol = document.getElementById('demoTestSymbol').value;
    const hint = document.getElementById('demoTestHint');
    if (!confirm(`Открыть тестовый ${{side.toUpperCase()}} по ${{symbol}} на Binance Demo?`)) return;
    document.querySelectorAll('[data-test-side]').forEach(item => item.disabled = true);
    hint.textContent = 'Отправляю рыночный Demo-ордер...';
    try {{
      const res = await fetch('/api/demo-test-order', {{
        method: 'POST',
        headers: {{
          'Content-Type': 'application/json',
          'X-Control-Token': document.getElementById('controlToken').value
        }},
        body: JSON.stringify({{symbol, side, confirmation: '{DEMO_TEST_CONFIRMATION}'}})
      }});
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || 'Demo-ордер не был открыт');
      hint.textContent = 'Demo-ордер открыт. Стоп и тейк выставлены.';
      await loadState();
    }} catch (error) {{
      hint.textContent = error.message;
    }} finally {{
      document.querySelectorAll('[data-test-side]').forEach(item => item.disabled = false);
    }}
  }});
}});

loadState();
setInterval(loadState, 10000);
</script>
</body>
</html>"""


def make_handler(controller: RuntimeController) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def current_ctx(self) -> BotContext:
            return controller.current()

        def mode_control(self) -> dict[str, Any]:
            is_local = self.client_address[0] in ("127.0.0.1", "::1")
            return controller.mode_control(is_local=is_local)

        def control_authorized(self) -> bool:
            expected = os.environ.get("DASHBOARD_CONTROL_TOKEN", "")
            supplied = self.headers.get("X-Control-Token", "")
            if expected:
                return secrets.compare_digest(supplied, expected)
            return self.client_address[0] in ("127.0.0.1", "::1")

        def read_json(self) -> dict[str, Any]:
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 8192)
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError("Некорректный запрос.") from exc
            if not isinstance(payload, dict):
                raise ValueError("Некорректный запрос.")
            return payload

        def do_GET(self) -> None:  # noqa: N802
            ctx = self.current_ctx()
            if self.path == "/health":
                with ctx.lock:
                    state = ensure_state(ctx)
                    broker_status = state.get("broker_status", {})
                send_json(
                    self,
                    {
                        "status": "ok",
                        "mode": ctx.mode,
                        "binance_connected": broker_status.get("connected", ctx.mode == "paper"),
                        "orders_enabled": ctx.orders_enabled,
                    },
                )
                return
            if self.path == "/api/config":
                safe_config = json.loads(json.dumps(ctx.config))
                send_json(self, safe_config)
                return
            if self.path == "/api/state":
                with ctx.lock:
                    state = ensure_state(ctx)
                    payload = public_state(ctx, state)
                payload["mode_control"] = self.mode_control()
                send_json(self, payload)
                return
            if self.path == "/" or self.path.startswith("/?"):
                send_html(self, dashboard_html(str(ctx.config["app"]["name"])))
                return
            send_json(self, {"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/scan":
                ctx = self.current_ctx()
                payload = scan_once(ctx)
                send_json(self, payload)
                return
            if self.path == "/api/mode":
                if not self.control_authorized():
                    send_json(
                        self,
                        {"error": "Неверный код управления или переключение разрешено только с этого компьютера."},
                        status=401,
                    )
                    return
                try:
                    request = self.read_json()
                    payload = controller.switch_mode(
                        str(request.get("mode", "")).lower(),
                        confirmation=str(request.get("confirmation", "")),
                    )
                except ValueError as exc:
                    send_json(self, {"error": str(exc)}, status=409)
                    return
                except Exception as exc:  # noqa: BLE001
                    send_json(self, {"error": f"Не удалось переключить режим: {exc}"}, status=500)
                    return
                send_json(self, payload)
                return
            if self.path == "/api/demo-test-order":
                if not self.control_authorized():
                    send_json(
                        self,
                        {"error": "Неверный код управления или действие разрешено только с этого компьютера."},
                        status=401,
                    )
                    return
                try:
                    request = self.read_json()
                    payload = open_demo_test_order(
                        self.current_ctx(),
                        str(request.get("symbol", "")),
                        str(request.get("side", "")),
                        str(request.get("confirmation", "")),
                    )
                except ValueError as exc:
                    send_json(self, {"error": str(exc)}, status=409)
                    return
                except Exception as exc:  # noqa: BLE001
                    send_json(self, {"error": f"Demo order failed: {exc}"}, status=500)
                    return
                send_json(self, payload)
                return
            send_json(self, {"error": "not found"}, status=404)

    return Handler


def build_context(
    config_path: Path,
    *,
    orders_enabled: bool = False,
    live_confirmation: str = "",
) -> BotContext:
    config = load_config(config_path)
    timezone = ZoneInfo(str(config["app"].get("timezone", "UTC")))
    data_dir = Path(str(config["app"].get("data_dir", "crypto_autobot/data")))
    broker_config = config.get("broker", {})
    mode = str(broker_config.get("mode", config.get("account", {}).get("mode", "paper"))).lower()
    if mode not in ("paper", "demo", "live"):
        raise ValueError("broker.mode must be paper, demo or live.")

    broker: BinanceFuturesBroker | None = None
    effective_orders_enabled = True
    if mode != "paper":
        key_prefix = "BINANCE_DEMO" if mode == "demo" else "BINANCE_LIVE"
        broker = BinanceFuturesBroker(
            environment=mode,
            api_key=os.environ.get(f"{key_prefix}_API_KEY", ""),
            secret_key=os.environ.get(f"{key_prefix}_API_SECRET", ""),
            recv_window_ms=int(broker_config.get("recv_window_ms", 5000)),
            orders_enabled=orders_enabled,
            live_confirmation=live_confirmation,
            quote_asset=str(broker_config.get("quote_asset", "USDT")),
            leverage=int(broker_config.get("leverage", 2)),
            margin_type=str(broker_config.get("margin_type", "ISOLATED")),
            working_type=str(broker_config.get("working_type", "MARK_PRICE")),
            price_protect=bool(broker_config.get("price_protect", False)),
        )
        config.setdefault("market", {})["base_url"] = broker.base_url
        effective_orders_enabled = orders_enabled

    state_name = "state.json" if mode == "paper" else f"state_{mode}.json"
    trades_name = "trades.csv" if mode == "paper" else f"trades_{mode}.csv"
    return BotContext(
        config=config,
        state_path=data_dir / state_name,
        trades_path=data_dir / trades_name,
        timezone=timezone,
        mode=mode,
        broker=broker,
        orders_enabled=effective_orders_enabled,
        exchange_snapshot={},
        lock=threading.Lock(),
        stop_event=threading.Event(),
    )


class RuntimeController:
    """Owns the active profile and switches it without mixing mode state."""

    PROFILE_FILES = {
        "paper": "config.example.json",
        "demo": "config.demo.example.json",
        "live": "config.live.example.json",
    }

    def __init__(
        self,
        initial_ctx: BotContext,
        config_path: Path,
        *,
        orders_enabled: bool,
        allow_live_ui: bool,
    ):
        self._ctx = initial_ctx
        self._lock = threading.RLock()
        self.orders_enabled = orders_enabled
        self.allow_live_ui = allow_live_ui
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()

        config_path = config_path.resolve()
        self.profile_paths = {
            mode: config_path.parent / filename for mode, filename in self.PROFILE_FILES.items()
        }
        self.profile_paths[initial_ctx.mode] = config_path

    def current(self) -> BotContext:
        with self._lock:
            return self._ctx

    def _availability(self, mode: str) -> tuple[bool, str]:
        path = self.profile_paths.get(mode)
        if path is None or not path.exists():
            return False, f"Не найден конфиг для режима {mode.upper()}."
        if mode == "demo":
            if not os.environ.get("BINANCE_DEMO_API_KEY") or not os.environ.get("BINANCE_DEMO_API_SECRET"):
                return False, "Сначала добавь BINANCE_DEMO_API_KEY и BINANCE_DEMO_API_SECRET."
        if mode == "live":
            if not self.allow_live_ui:
                return False, "Live заблокирован. Запусти бота с --allow-live-ui."
            if not os.environ.get("BINANCE_LIVE_API_KEY") or not os.environ.get("BINANCE_LIVE_API_SECRET"):
                return False, "Сначала добавь BINANCE_LIVE_API_KEY и BINANCE_LIVE_API_SECRET."
        return True, ""

    def mode_control(self, *, is_local: bool) -> dict[str, Any]:
        requires_token = bool(os.environ.get("DASHBOARD_CONTROL_TOKEN"))
        control_available = is_local or requires_token
        options: dict[str, Any] = {}
        for mode in ("paper", "demo", "live"):
            available, reason = self._availability(mode)
            if mode == "paper":
                summary = "Виртуальный баланс, реальные ордера не отправляются."
            elif mode == "demo":
                order_text = "разрешены" if self.orders_enabled else "заблокированы при запуске"
                summary = f"Binance Demo, тестовые ордера {order_text}."
            else:
                order_text = "разрешены" if self.orders_enabled else "заблокированы при запуске"
                summary = f"Реальный Binance, ордера {order_text}."
            options[mode] = {
                "available": available,
                "reason": reason,
                "summary": summary,
            }
        return {
            "options": options,
            "requires_token": requires_token,
            "control_available": control_available,
            "control_reason": (
                ""
                if control_available
                else "На сервере задай DASHBOARD_CONTROL_TOKEN, чтобы управлять режимом через интерфейс."
            ),
            "live_confirmation": LIVE_CONFIRMATION,
        }

    def switch_mode(self, mode: str, *, confirmation: str = "") -> dict[str, Any]:
        if mode not in ("paper", "demo", "live"):
            raise ValueError("Выбери Paper, Demo или Live.")

        with self._lock:
            current = self._ctx
            if mode == current.mode:
                return {"status": "ok", "mode": mode, "changed": False}

            available, reason = self._availability(mode)
            if not available:
                raise ValueError(reason)
            if mode == "live" and confirmation != LIVE_CONFIRMATION:
                raise ValueError(f"Для Live введи точную фразу: {LIVE_CONFIRMATION}")

            with current.lock:
                current_state = ensure_state(current)
                managed_positions = current_state.get("positions", {})
                if managed_positions:
                    symbols = ", ".join(sorted(managed_positions))
                    raise ValueError(
                        "Нельзя сменить режим, пока бот ведёт открытую позицию: "
                        f"{symbols}. Дождись её закрытия."
                    )

            target = build_context(
                self.profile_paths[mode],
                orders_enabled=self.orders_enabled,
                live_confirmation=confirmation,
            )
            with target.lock:
                target_state = ensure_state(target)
                target_state["mode"] = mode
                target_state["broker_status"] = {
                    "mode": mode,
                    "connected": mode == "paper",
                    "orders_enabled": target.orders_enabled,
                }
                log_event(
                    target_state,
                    f"mode switched: {current.mode.upper()} -> {mode.upper()}",
                    target.timezone,
                )
                write_state(target, target_state)

            self._ctx = target
            self.wake_event.set()
            return {"status": "ok", "mode": mode, "changed": True}


def run_server(controller: RuntimeController) -> None:
    ctx = controller.current()
    host = str(ctx.config["app"].get("host", "0.0.0.0"))
    port = int(ctx.config["app"].get("port", 8090))
    thread = threading.Thread(target=worker_loop, args=(controller,), daemon=True)
    thread.start()
    server = ThreadingHTTPServer((host, port), make_handler(controller))
    print(f"{ctx.config['app']['name']} listening on http://{host}:{port}")
    print("Dashboard: http://127.0.0.1:%d" % port)
    print(f"Mode: {ctx.mode.upper()}; orders: {'ENABLED' if ctx.orders_enabled else 'DISABLED'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        controller.stop_event.set()
        controller.wake_event.set()
        server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crypto Autobot: paper and Binance Futures trading.")
    parser.add_argument("--config", default="crypto_autobot/config.example.json")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--check", action="store_true", help="check Binance credentials without placing orders")
    parser.add_argument("--enable-orders", action="store_true", help="allow Binance order placement")
    parser.add_argument(
        "--allow-live-ui",
        action="store_true",
        help="allow the dashboard to switch to the live Binance profile",
    )
    parser.add_argument(
        "--confirm-live",
        default="",
        help=f"live-only safety phrase: {LIVE_CONFIRMATION}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ctx = build_context(
        Path(args.config),
        orders_enabled=args.enable_orders,
        live_confirmation=args.confirm_live,
    )
    ensure_trades_file(ctx)
    state = ensure_state(ctx)
    if args.check:
        if ctx.broker is None:
            print(json.dumps({"status": "ok", "mode": "paper", "message": "No Binance keys required."}, indent=2))
            return 0
        summary = ctx.broker.account_summary()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "environment": summary["environment"],
                    "asset": summary["asset"],
                    "balance": summary["balance"],
                    "available_balance": summary["available_balance"],
                    "open_positions": len(summary["positions"]),
                    "position_mode": summary["position_mode"],
                    "orders_enabled": summary["orders_enabled"],
                },
                indent=2,
            )
        )
        return 0
    if args.once:
        payload = scan_once(ctx)
        for result in payload["results"]:
            print(f"{result.get('symbol')}: {result.get('status')}")
        return 0
    controller = RuntimeController(
        ctx,
        Path(args.config),
        orders_enabled=args.enable_orders,
        allow_live_ui=args.allow_live_ui,
    )
    run_server(controller)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
