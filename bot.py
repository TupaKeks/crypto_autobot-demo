#!/usr/bin/env python3
"""24/7 crypto strategy bot with paper, Binance Demo and live modes."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .broker_interface import BrokerAdapter
    from .binance_futures import BinanceFuturesBroker, LIVE_CONFIRMATION
    from .forward_validation import forward_validation_report
    from .mt5_broker import MT5Broker
    from .strategy_intraday import build_indicators as build_intraday_indicators
    from .strategy_intraday import evaluate_strategy_signal, minimum_history as intraday_minimum_history
    from .orderflow_model import evaluate_orderflow_signal, model_status as orderflow_model_status
except ImportError:
    from broker_interface import BrokerAdapter
    from binance_futures import BinanceFuturesBroker, LIVE_CONFIRMATION
    from forward_validation import forward_validation_report
    from mt5_broker import MT5Broker
    from strategy_intraday import build_indicators as build_intraday_indicators
    from strategy_intraday import evaluate_strategy_signal, minimum_history as intraday_minimum_history
    from orderflow_model import evaluate_orderflow_signal, model_status as orderflow_model_status


DEMO_TEST_CONFIRMATION = "DEMO_MARKET_TEST"
INTRADAY_STRATEGIES = {
    "intraday_pullback",
    "intraday_mean_reversion",
    "intraday_breakout",
    "intraday_regime_pullback",
    "intraday_liquidity_sweep",
}
ROOT = Path(__file__).resolve().parent
VALIDATION_DAILY_KEYS = {
    "validation_pnls",
    "validation_realized_rs",
    "validation_closed",
    "validation_wins",
    "validation_losses",
    "validation_gross_profit",
    "validation_gross_loss",
    "validation_realized_pnl",
}
STATE_BACKUP_GENERATIONS = 2
_STATE_BACKUP_SIGNATURES: dict[str, str] = {}


@dataclasses.dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float = 0.0
    trade_count: int = 0
    taker_buy_volume: float = 0.0
    taker_buy_quote_volume: float = 0.0

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
    broker: BrokerAdapter | None
    orders_enabled: bool
    exchange_snapshot: dict[str, Any]
    lock: threading.Lock
    stop_event: threading.Event


def broker_provider(ctx: BotContext) -> str:
    if ctx.broker is None:
        return "paper"
    return str(ctx.config.get("broker", {}).get("provider", "binance")).lower()


def broker_name(ctx: BotContext) -> str:
    return "MT5" if broker_provider(ctx) == "mt5" else "Binance"


def market_data_environment(ctx: BotContext) -> str:
    if broker_provider(ctx) == "mt5":
        return "MT5 terminal"
    host = urllib.parse.urlparse(
        str(ctx.config.get("market", {}).get("base_url", ""))
    ).netloc.lower()
    if host == "fapi.binance.com":
        return "Binance Production"
    if host == "demo-fapi.binance.com":
        return "Binance Demo"
    return host or "unknown"


def merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key == "extends":
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)
    if config.get("extends"):
        parent = load_config(path.parent / str(config["extends"]))
        config = merge_config(parent, config)
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


def execution_diagnostics(state: dict[str, Any], timezone: ZoneInfo) -> dict[str, Any]:
    diagnostics = state.get("execution_diagnostics")
    if diagnostics is not None:
        return diagnostics

    logs = state.get("logs", [])
    diagnostics = {
        "started_at": state.get("created_at") or now_iso(timezone),
        "candles_observed": 0,
        "status_counts": {},
        "signal_orders": sum(
            ": placed " in str(item.get("message", ""))
            and " limit on " in str(item.get("message", ""))
            for item in logs
        ),
        "market_entries": 0,
        "limit_fills": 0,
        "limit_expired": sum(
            "limit entry expired" in str(item.get("message", "")).lower()
            for item in logs
        ),
        "limit_canceled": 0,
        "last_candle_by_symbol": {},
    }
    state["execution_diagnostics"] = diagnostics
    return diagnostics


def diagnostic_status_bucket(status: str) -> str:
    value = status.strip().lower()
    if value.startswith("placed "):
        return "signal_order"
    if value.startswith("opened "):
        return "market_entry"
    if "limit pending" in value:
        return "limit_pending"
    if value.startswith("limit filled") or value == "position open":
        return "position_open"
    if "stale market data" in value:
        return "stale_data"
    if "error" in value:
        return "error"
    if "filter" in value or value in {
        "no signal",
        "outside baseline universe",
        "strategy disabled",
        "latest candle already processed",
    }:
        return "no_signal"
    return "other"


def record_scan_diagnostic(
    state: dict[str, Any],
    result: dict[str, Any],
    timezone: ZoneInfo,
) -> None:
    symbol = str(result.get("symbol", "")).upper()
    candle_open_time = result.get("candle_open_time")
    if not symbol or candle_open_time is None:
        return

    diagnostics = execution_diagnostics(state, timezone)
    candle_key = str(int(candle_open_time))
    last_candles = diagnostics.setdefault("last_candle_by_symbol", {})
    if last_candles.get(symbol) == candle_key:
        return

    last_candles[symbol] = candle_key
    diagnostics["candles_observed"] = int(diagnostics.get("candles_observed", 0)) + 1
    bucket = diagnostic_status_bucket(str(result.get("status", "")))
    counts = diagnostics.setdefault("status_counts", {})
    counts[bucket] = int(counts.get(bucket, 0)) + 1
    if bucket not in {"stale_data", "error"}:
        candle_date = dt.datetime.fromtimestamp(
            int(candle_open_time) / 1000,
            timezone,
        ).date().isoformat()
        coverage = state.setdefault("validation_coverage", {})
        day = coverage.setdefault(candle_date, {"symbol_candles": 0})
        day["symbol_candles"] = int(day.get("symbol_candles", 0)) + 1
        day["updated_at"] = now_iso(timezone)


def validation_trade_date(row: dict[str, Any], timezone: ZoneInfo) -> str:
    explicit_date = str(row.get("validation_date", ""))
    try:
        return dt.date.fromisoformat(explicit_date).isoformat()
    except ValueError:
        pass
    value = str(row.get("time", ""))
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.now(timezone)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone).date().isoformat()


def record_validation_close(
    day: dict[str, Any],
    pnl: float,
    realized_r: float | None = None,
) -> None:
    day.setdefault("validation_pnls", []).append(float(pnl))
    if realized_r is not None and math.isfinite(realized_r):
        day.setdefault("validation_realized_rs", []).append(float(realized_r))
    day["validation_closed"] = int(day.get("validation_closed", 0)) + 1
    if pnl > 0:
        day["validation_wins"] = int(day.get("validation_wins", 0)) + 1
        day["validation_gross_profit"] = float(day.get("validation_gross_profit", 0.0)) + pnl
    elif pnl < 0:
        day["validation_losses"] = int(day.get("validation_losses", 0)) + 1
        day["validation_gross_loss"] = float(day.get("validation_gross_loss", 0.0)) + abs(pnl)
    day["validation_realized_pnl"] = float(day.get("validation_realized_pnl", 0.0)) + pnl


def ensure_validation_daily_history(state: dict[str, Any], timezone: ZoneInfo) -> None:
    version = int(state.get("validation_daily_version", 0))
    if version >= 2:
        return

    daily = state.setdefault("daily", {})
    if version == 1:
        for day in daily.values():
            if isinstance(day, dict):
                day.pop("validation_realized_rs", None)
        for row in state.get("trades", []):
            if (
                str(row.get("event", "")) != "close"
                or str(row.get("source", "baseline")) == "manual_demo_test"
                or row.get("realized_r") is None
            ):
                continue
            date_key = validation_trade_date(row, timezone)
            day = daily.setdefault(date_key, {"trades": 0, "realized_pnl": 0.0})
            realized_r = float(row["realized_r"])
            if math.isfinite(realized_r):
                day.setdefault("validation_realized_rs", []).append(realized_r)
        state["validation_daily_version"] = 2
        return

    for day in daily.values():
        if isinstance(day, dict):
            for key in VALIDATION_DAILY_KEYS:
                day.pop(key, None)
    for row in state.get("trades", []):
        if (
            str(row.get("event", "")) != "close"
            or str(row.get("source", "baseline")) == "manual_demo_test"
        ):
            continue
        date_key = validation_trade_date(row, timezone)
        day = daily.setdefault(date_key, {"trades": 0, "realized_pnl": 0.0})
        realized_r = row.get("realized_r")
        record_validation_close(
            day,
            float(row.get("pnl", 0.0)),
            float(realized_r) if realized_r is not None else None,
        )
    state["validation_daily_version"] = 2


def validation_profile_payload(ctx: BotContext) -> dict[str, Any]:
    broker = ctx.config.get("broker", {})
    return {
        "market": ctx.config.get("market", {}),
        "strategy": ctx.config.get("strategy", {}),
        "ensemble": ctx.config.get("ensemble", {}),
        "account": ctx.config.get("account", {}),
        "broker": {
            key: broker.get(key)
            for key in (
                "provider",
                "mode",
                "leverage",
                "margin_type",
                "working_type",
                "price_protect",
                "paper_maker_fee_bps",
                "paper_taker_fee_bps",
                "paper_slippage_bps",
            )
            if key in broker
        },
    }


def validation_profile_hash(ctx: BotContext) -> str:
    encoded = json.dumps(
        validation_profile_payload(ctx),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reset_validation_evidence(
    state: dict[str, Any],
    ctx: BotContext,
    *,
    previous_hash: str | None,
) -> None:
    reset_at = now_iso(ctx.timezone)
    state["validation_started_at"] = reset_at
    state["validation_coverage"] = {}
    state["validation_active_dates"] = []
    for day in state.setdefault("daily", {}).values():
        if not isinstance(day, dict):
            continue
        day["validation_trades"] = 0
        for key in VALIDATION_DAILY_KEYS:
            day.pop(key, None)
    state["validation_daily_version"] = 2
    state["execution_diagnostics"] = {
        "started_at": reset_at,
        "candles_observed": 0,
        "status_counts": {},
        "signal_orders": 0,
        "market_entries": 0,
        "limit_fills": 0,
        "limit_expired": 0,
        "limit_canceled": 0,
        "last_candle_by_symbol": {},
    }
    state["validation_profile_reset"] = {
        "at": reset_at,
        "reason": "validation profile changed",
        "previous_hash": previous_hash,
    }
    state.setdefault("logs", []).append(
        {
            "time": reset_at,
            "message": "Demo validation restarted: strategy or market-data profile changed",
        }
    )
    state["logs"] = state["logs"][-120:]


def ensure_validation_profile(state: dict[str, Any], ctx: BotContext) -> None:
    current_hash = validation_profile_hash(ctx)
    previous_hash = state.get("validation_profile_hash")
    has_evidence = bool(
        state.get("validation_coverage")
        or state.get("validation_active_dates")
        or any(
            isinstance(day, dict)
            and (
                int(day.get("validation_trades", 0)) > 0
                or any(key in day for key in VALIDATION_DAILY_KEYS)
            )
            for day in state.get("daily", {}).values()
        )
    )
    if previous_hash != current_hash and (previous_hash is not None or has_evidence):
        reset_validation_evidence(
            state,
            ctx,
            previous_hash=str(previous_hash) if previous_hash is not None else None,
        )
    state["validation_profile_hash"] = current_hash
    state["validation_profile"] = validation_profile_payload(ctx)


def state_backup_paths(state_path: Path) -> list[Path]:
    return [
        state_path.with_name(f"{state_path.name}.bak{generation}")
        for generation in range(1, STATE_BACKUP_GENERATIONS + 1)
    ]


def _load_state_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise ValueError(f"State file must contain a JSON object: {path}")
    return payload


def _fsync_parent(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path.parent), flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as target:
            json.dump(payload, target, indent=2, sort_keys=True)
            target.flush()
            os.fsync(target.fileno())
        tmp.replace(path)
        _fsync_parent(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _state_durability_signature(state: dict[str, Any]) -> str:
    volatile_keys = {
        "updated_at",
        "runtime",
        "latest",
        "logs",
        "broker_status",
        "exchange_positions",
        "execution_diagnostics",
    }
    durable = {key: value for key, value in state.items() if key not in volatile_keys}
    encoded = json.dumps(durable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _refresh_state_backups(ctx: BotContext, state: dict[str, Any]) -> None:
    signature = _state_durability_signature(state)
    signature_key = str(ctx.state_path.resolve())
    backups = state_backup_paths(ctx.state_path)
    if _STATE_BACKUP_SIGNATURES.get(signature_key) == signature and backups[0].exists():
        return

    if backups[0].exists():
        try:
            previous = _load_state_json(backups[0])
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            previous = None
        if previous is not None:
            _atomic_write_json(backups[1], previous)
    _atomic_write_json(backups[0], state)
    _STATE_BACKUP_SIGNATURES[signature_key] = signature


def _recover_state(ctx: BotContext, original_error: Exception) -> dict[str, Any]:
    recovered: dict[str, Any] | None = None
    source_path: Path | None = None
    for backup_path in state_backup_paths(ctx.state_path):
        try:
            recovered = _load_state_json(backup_path)
        except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            continue
        source_path = backup_path
        break
    if recovered is None or source_path is None:
        raise original_error

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    corrupt_path = ctx.state_path.with_name(f"{ctx.state_path.name}.corrupt-{stamp}")
    ctx.state_path.replace(corrupt_path)
    recovery_at = now_iso(ctx.timezone)
    recovered["state_recovery"] = {
        "at": recovery_at,
        "source": source_path.name,
        "corrupt_file": corrupt_path.name,
    }
    recovered.setdefault("logs", []).append(
        {
            "time": recovery_at,
            "message": f"State restored from {source_path.name}; corrupt file archived",
        }
    )
    recovered["logs"] = recovered["logs"][-120:]
    try:
        _atomic_write_json(ctx.state_path, recovered)
        _refresh_state_backups(ctx, recovered)
    except Exception:
        if not ctx.state_path.exists() and corrupt_path.exists():
            corrupt_path.replace(ctx.state_path)
        raise
    return recovered


def ensure_state(ctx: BotContext) -> dict[str, Any]:
    ctx.state_path.parent.mkdir(parents=True, exist_ok=True)
    if ctx.state_path.exists():
        try:
            state = _load_state_json(ctx.state_path)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            state = _recover_state(ctx, exc)
        ensure_validation_daily_history(state, ctx.timezone)
        ensure_validation_profile(state, ctx)
        return state

    balance = float(ctx.config["account"]["initial_balance"])
    state = {
        "created_at": now_iso(ctx.timezone),
        "updated_at": now_iso(ctx.timezone),
        "mode": ctx.mode,
        "balance": balance,
        "initial_balance": balance,
        "realized_pnl": 0.0,
        "positions": {},
        "pending_entries": {},
        "exchange_positions": {},
        "broker_status": {
            "mode": ctx.mode,
            "provider": broker_provider(ctx),
            "name": broker_name(ctx) if ctx.broker is not None else "Paper",
            "connected": ctx.mode == "paper",
            "orders_enabled": ctx.orders_enabled,
        },
        "trades": [],
        "daily": {},
        "seen_signal_candles": {},
        "latest": {},
        "logs": [],
        "validation_coverage": {},
        "validation_daily_version": 2,
        "execution_diagnostics": {
            "started_at": now_iso(ctx.timezone),
            "candles_observed": 0,
            "status_counts": {},
            "signal_orders": 0,
            "market_entries": 0,
            "limit_fills": 0,
            "limit_expired": 0,
            "limit_canceled": 0,
            "last_candle_by_symbol": {},
        },
        "runtime": {
            "scan_sequence": 0,
            "scan_in_progress": False,
            "consecutive_failures": 0,
        },
    }
    ensure_validation_profile(state, ctx)
    write_state(ctx, state)
    return state


def write_state(ctx: BotContext, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso(ctx.timezone)
    _atomic_write_json(ctx.state_path, state)
    _refresh_state_backups(ctx, state)


def normalize_broker_position(position: dict[str, Any]) -> dict[str, Any]:
    """Convert Binance or MT5 payloads to the runtime position contract."""
    if "positionAmt" in position:
        amount = float(position.get("positionAmt", 0.0))
        return {
            "symbol": str(position.get("symbol", "")),
            "side": "long" if amount > 0 else "short",
            "quantity": abs(amount),
            "entry": float(position.get("entryPrice", 0.0)),
            "stop": float(position.get("stop", 0.0)),
            "target": float(position.get("target", 0.0)),
            "unrealized_pnl": float(position.get("unRealizedProfit", 0.0)),
            "raw": position,
        }
    normalized = dict(position)
    normalized.update(
        {
            "symbol": str(position.get("symbol", "")),
            "side": str(position.get("side", "")),
            "quantity": abs(float(position.get("quantity", 0.0))),
            "entry": float(position.get("entry", 0.0)),
            "stop": float(position.get("stop", 0.0)),
            "target": float(position.get("target", 0.0)),
            "unrealized_pnl": float(position.get("unrealized_pnl", 0.0)),
        }
    )
    return normalized


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
    ensure_validation_daily_history(state, ctx.timezone)
    state.setdefault("trades", []).append(row)
    if (
        str(row.get("event", "")) == "close"
        and str(row.get("source", "baseline")) != "manual_demo_test"
    ):
        date_key = validation_trade_date(row, ctx.timezone)
        day = state.setdefault("daily", {}).setdefault(
            date_key,
            {"trades": 0, "realized_pnl": 0.0},
        )
        realized_r = row.get("realized_r")
        record_validation_close(
            day,
            float(row.get("pnl", 0.0)),
            float(realized_r) if realized_r is not None else None,
        )
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
    path = "/fapi/v1/klines"
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
            quote_volume=float(row[7]) if len(row) > 7 else 0.0,
            trade_count=int(row[8]) if len(row) > 8 else 0,
            taker_buy_volume=float(row[9]) if len(row) > 9 else 0.0,
            taker_buy_quote_volume=float(row[10]) if len(row) > 10 else 0.0,
        )
        if candle.close_time <= now_ms:
            candles.append(candle)
    return candles


def fetch_market_candles(ctx: BotContext, symbol: str) -> list[Candle]:
    market = ctx.config["market"]
    interval = str(market["interval"])
    limit = int(market["history_limit"])
    if broker_provider(ctx) != "mt5":
        return fetch_klines(str(market["base_url"]), symbol, interval, limit)
    if ctx.broker is None:
        raise RuntimeError("MT5 market data requires an initialized MT5 broker.")

    rows = ctx.broker.fetch_candles(symbol, interval, limit)
    return [
        Candle(
            open_time=int(row["open_time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0)),
            close_time=int(row["close_time"]),
            quote_volume=float(row.get("quote_volume", 0.0)),
            trade_count=int(row.get("trade_count", 0)),
            taker_buy_volume=float(row.get("taker_buy_volume", 0.0)),
            taker_buy_quote_volume=float(row.get("taker_buy_quote_volume", 0.0)),
        )
        for row in rows
    ]


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


def position_realized_r(position: dict[str, Any], pnl: float) -> float | None:
    entry = float(position.get("entry", 0.0))
    initial_stop = float(position.get("initial_stop", position.get("stop", 0.0)))
    quantity = abs(float(position.get("qty", position.get("quantity", 0.0))))
    initial_risk = abs(entry - initial_stop) * quantity
    if initial_risk <= 0 or not math.isfinite(initial_risk):
        return None
    realized_r = float(pnl) / initial_risk
    return realized_r if math.isfinite(realized_r) else None


def can_trade(state: dict[str, Any], ctx: BotContext) -> tuple[bool, str]:
    account = ctx.config["account"]
    if ctx.mode != "paper":
        if not ctx.orders_enabled:
            return False, f"{broker_name(ctx)} orders disabled"
        if not state.get("broker_status", {}).get("connected"):
            return False, f"{broker_name(ctx)} disconnected"

    daily = daily_stats(state, ctx)
    if ctx.mode != "paper" and float(state.get("balance", 0.0)) <= 0:
        return False, "Demo balance is zero; add virtual USDT first"
    if int(daily["trades"]) >= int(account["max_daily_trades"]):
        return False, "daily trade limit reached"

    initial = float(state.get("initial_balance", account["initial_balance"]))
    max_loss = initial * float(account["max_daily_loss_percent"]) / 100.0
    if float(daily.get("realized_pnl", 0.0)) <= -max_loss:
        return False, "daily loss limit reached"

    reserved_slots = len(state.get("positions", {})) + len(state.get("pending_entries", {}))
    if reserved_slots >= int(account["max_open_positions"]):
        return False, "max open positions reached"

    return True, "ok"


def calc_qty(balance: float, entry: float, stop: float, risk_percent: float) -> float:
    risk_cash = balance * risk_percent / 100.0
    risk_per_unit = abs(entry - stop)
    if risk_cash <= 0 or risk_per_unit <= 0:
        return 0.0
    return risk_cash / risk_per_unit


def side_risk_percent(account: dict[str, Any], side: str) -> float:
    key = "long_risk_per_trade_percent" if side == "long" else "short_risk_per_trade_percent"
    return float(account.get(key, account["risk_per_trade_percent"]))


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

    broker_config = ctx.config.get("broker", {})
    maker_fee_rate = float(broker_config.get("paper_maker_fee_bps", 0.0)) / 10_000.0
    taker_fee_rate = float(broker_config.get("paper_taker_fee_bps", 0.0)) / 10_000.0
    slippage = float(broker_config.get("paper_slippage_bps", 0.0)) / 10_000.0
    target_fill = str(position.get("target_order_type", "market")) == "limit" and "target" in reason
    exit_side = "sell" if position["side"] == "long" else "buy"
    exit_price = float(price)
    if not target_fill:
        exit_price *= 1 - slippage if exit_side == "sell" else 1 + slippage
    gross_pnl = position_unrealized(position, exit_price)
    exit_fee_rate = maker_fee_rate if target_fill else taker_fee_rate
    exit_fee = exit_price * float(position["qty"]) * exit_fee_rate
    entry_fee = float(position.get("entry_fee", 0.0))
    pnl = gross_pnl - entry_fee - exit_fee
    state["balance"] = float(state["balance"]) + gross_pnl - exit_fee
    state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + pnl
    daily = daily_stats(state, ctx)
    daily["realized_pnl"] = float(daily.get("realized_pnl", 0.0)) + pnl

    row = {
        "time": now_iso(ctx.timezone),
        "event": "close",
        "symbol": symbol,
        "side": position["side"],
        "qty": round(float(position["qty"]), 8),
        "price": round(exit_price, 8),
        "stop": round(float(position["stop"]), 8),
        "target": round(float(position["target"]), 8),
        "pnl": round(pnl, 2),
        "realized_r": position_realized_r(position, pnl),
        "balance": round(float(state["balance"]), 2),
        "reason": reason,
        "source": str(position.get("source", "baseline")),
        "validation_date": validation_trade_date(
            {"time": position.get("opened_at")},
            ctx.timezone,
        ),
    }
    append_trade(ctx, state, row)
    log_event(
        state,
        f"{symbol}: closed {position['side']} at {exit_price:.6g}, pnl={pnl:.2f} ({reason})",
        ctx.timezone,
    )
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
    entry_override: float | None = None,
    broker_result_override: dict[str, Any] | None = None,
    trade_profile: dict[str, Any] | None = None,
) -> None:
    account = ctx.config["account"]
    strategy = ctx.config["strategy"]
    profile = {**strategy, **(trade_profile or {})}
    risk_percent = side_risk_percent(account, side)
    entry = candle.close if entry_override is None else float(entry_override)

    if side == "long":
        stop = entry - atr_value * float(profile["stop_atr"])
        target = entry + atr_value * float(profile["target_atr"])
    else:
        stop = entry + atr_value * float(profile["stop_atr"])
        target = entry - atr_value * float(profile["target_atr"])

    broker_result = broker_result_override
    if broker_result is None and ctx.broker is not None:
        broker_result = ctx.broker.open_position(
            symbol=symbol,
            side=side,
            stop_distance=abs(entry - stop),
            target_distance=abs(target - entry),
            risk_percent=risk_percent,
            max_open_positions=int(account["max_open_positions"]),
        )
    if broker_result is not None:
        entry = float(broker_result["entry"])
        stop = float(broker_result["stop"])
        target = float(broker_result["target"])
        qty = float(broker_result["quantity"])
        state["balance"] = float(broker_result["wallet_balance"])
    else:
        qty = calc_qty(float(state["balance"]), entry, stop, risk_percent)
        if qty <= 0 or not math.isfinite(qty):
            log_event(state, f"{symbol}: blocked, invalid position size", ctx.timezone)
            return
        broker_config = ctx.config.get("broker", {})
        entry_is_maker = str(profile.get("entry_order_type", "market")) == "limit_retrace"
        entry_fee_bps = float(
            broker_config.get(
                "paper_maker_fee_bps" if entry_is_maker else "paper_taker_fee_bps",
                0.0,
            )
        )
        entry_fee = entry * qty * entry_fee_bps / 10_000.0
        state["balance"] = float(state["balance"]) - entry_fee

    market_interval = str(ctx.config.get("market", {}).get("interval", "15m"))
    position = {
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "qty": qty,
        "stop": stop,
        "initial_stop": stop,
        "target": target,
        "opened_at": now_iso(ctx.timezone),
        "opened_candle_time": (
            int(broker_result.get("opened_at_ms"))
            // interval_milliseconds(market_interval)
            * interval_milliseconds(market_interval)
            if broker_result and broker_result.get("opened_at_ms")
            else candle.open_time
        ),
        "reason": reason,
        "highest": candle.high,
        "lowest": candle.low,
        "mode": ctx.mode,
        "entry_fee": entry_fee if broker_result is None else 0.0,
        "risk_percent": risk_percent,
        "source": str(profile.get("source", "baseline")),
        "target_order_type": str(profile.get("target_order_type", "market")),
        "max_holding_bars": int(profile.get("max_holding_bars", 0)),
        "trail_after_r": float(profile.get("trail_after_r", 99.0)),
        "trail_atr": float(profile.get("trail_atr", 1.5)),
    }
    if broker_result:
        position.update(
            {
                "entry_order_id": broker_result.get("entry_order_id"),
                "stop_algo_id": broker_result.get("stop_algo_id"),
                "target_algo_id": broker_result.get("target_algo_id"),
                "target_order_id": broker_result.get("target_order_id"),
                "target_order_type": broker_result.get("target_order_type", "market"),
                "opened_at_ms": broker_result.get("opened_at_ms"),
            }
        )
    state.setdefault("positions", {})[symbol] = position
    daily = daily_stats(state, ctx)
    daily["trades"] = int(daily.get("trades", 0)) + 1
    daily.setdefault("validation_trades", 0)
    if position["source"] != "manual_demo_test":
        daily["validation_trades"] = int(daily.get("validation_trades", 0)) + 1
    if position["source"] == "orderflow_ml":
        daily["orderflow_ml_trades"] = int(daily.get("orderflow_ml_trades", 0)) + 1

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
        "source": position["source"],
    }
    append_trade(ctx, state, row)
    destination = broker_name(ctx) if ctx.broker else "paper"
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


def interval_milliseconds(interval: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    unit = interval[-1]
    if unit not in units:
        raise ValueError(f"Unsupported interval: {interval}")
    return int(interval[:-1]) * units[unit]


def market_data_is_fresh(
    candle: Candle,
    interval: str,
    max_age_intervals: float,
    now_ms: int | None = None,
) -> bool:
    if max_age_intervals <= 0:
        return True
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    max_age_ms = interval_milliseconds(interval) * float(max_age_intervals)
    return current_ms - int(candle.close_time) <= max_age_ms


def place_pending_entry(
    ctx: BotContext,
    state: dict[str, Any],
    symbol: str,
    side: str,
    candle: Candle,
    atr_value: float,
    reason: str,
    trade_profile: dict[str, Any] | None = None,
) -> None:
    strategy = ctx.config["strategy"]
    profile = {**strategy, **(trade_profile or {})}
    account = ctx.config["account"]
    offset = atr_value * float(profile.get("entry_offset_atr", 0.0))
    limit_price = candle.close - offset if side == "long" else candle.close + offset
    stop_distance = atr_value * float(profile["stop_atr"])
    target_distance = atr_value * float(profile["target_atr"])
    expiry_bars = max(1, int(profile.get("entry_expiry_bars", 1)))
    expiry_open_time = candle.open_time + interval_milliseconds(str(ctx.config["market"]["interval"])) * expiry_bars
    broker_result: dict[str, Any] | None = None
    if ctx.broker is not None:
        broker_result = ctx.broker.place_limit_entry(
            symbol=symbol,
            side=side,
            limit_price=limit_price,
            stop_distance=stop_distance,
            target_distance=target_distance,
            risk_percent=side_risk_percent(account, side),
            max_open_positions=int(account["max_open_positions"]),
        )
        limit_price = float(broker_result["limit_price"])
        state["balance"] = float(broker_result["wallet_balance"])
    state.setdefault("pending_entries", {})[symbol] = {
        "symbol": symbol,
        "side": side,
        "limit_price": limit_price,
        "stop_distance": stop_distance,
        "target_distance": target_distance,
        "signal_candle_time": candle.open_time,
        "expiry_open_time": expiry_open_time,
        "atr": atr_value,
        "reason": reason,
        "trade_profile": trade_profile or {},
        "entry_order_id": broker_result.get("entry_order_id") if broker_result else None,
        "entry_client_order_id": broker_result.get("entry_client_order_id") if broker_result else None,
        "placed_at": now_iso(ctx.timezone),
    }
    diagnostics = execution_diagnostics(state, ctx.timezone)
    diagnostics["signal_orders"] = int(diagnostics.get("signal_orders", 0)) + 1
    destination = broker_name(ctx) if ctx.broker else "paper"
    log_event(state, f"{symbol}: placed {side} limit on {destination} at {limit_price:.6g}", ctx.timezone)
    write_state(ctx, state)


def reconcile_pending_entry(
    ctx: BotContext,
    state: dict[str, Any],
    symbol: str,
    candles: list[Candle],
) -> str | None:
    pending = state.setdefault("pending_entries", {}).get(symbol)
    if not pending:
        return None
    latest = candles[-1]
    filled_candle = latest
    broker_result: dict[str, Any] | None = None

    if ctx.broker is not None:
        client_order_id = str(pending["entry_client_order_id"])
        order = ctx.broker.get_entry_order(symbol, client_order_id)
        status = str(order.get("status", "UNKNOWN")).upper()
        # A signal is known only after its candle closes. At expiry_open_time the
        # live limit has just entered its one eligible retrace candle; cancel it
        # only when the following candle begins. Paper reconciliation sees the
        # completed eligible candle and can expire at equality below.
        if status != "FILLED" and latest.open_time > int(pending["expiry_open_time"]):
            order = ctx.broker.cancel_entry_order(symbol, client_order_id)
            status = str(order.get("status", "UNKNOWN")).upper()
            if status != "FILLED":
                state["pending_entries"].pop(symbol, None)
                diagnostics = execution_diagnostics(state, ctx.timezone)
                diagnostics["limit_expired"] = int(diagnostics.get("limit_expired", 0)) + 1
                log_event(state, f"{symbol}: limit entry expired ({status})", ctx.timezone)
                return "limit entry expired"
        if status != "FILLED":
            if status in {"CANCELED", "EXPIRED", "REJECTED"}:
                state["pending_entries"].pop(symbol, None)
                diagnostics = execution_diagnostics(state, ctx.timezone)
                diagnostics["limit_canceled"] = int(diagnostics.get("limit_canceled", 0)) + 1
                log_event(state, f"{symbol}: limit entry {status.lower()}", ctx.timezone)
                return f"limit entry {status.lower()}"
            return f"limit pending at {float(pending['limit_price']):.6g}"
        broker_result = ctx.broker.activate_limit_entry(
            symbol=symbol,
            side=str(pending["side"]),
            client_order_id=client_order_id,
            stop_distance=float(pending["stop_distance"]),
            target_distance=float(pending["target_distance"]),
        )
    else:
        eligible = [
            item
            for item in candles
            if int(pending["signal_candle_time"]) < item.open_time <= int(pending["expiry_open_time"])
        ]
        limit_price = float(pending["limit_price"])
        for item in eligible:
            touched = item.low <= limit_price if pending["side"] == "long" else item.high >= limit_price
            if touched:
                filled_candle = item
                break
        else:
            if latest.open_time >= int(pending["expiry_open_time"]):
                state["pending_entries"].pop(symbol, None)
                diagnostics = execution_diagnostics(state, ctx.timezone)
                diagnostics["limit_expired"] = int(diagnostics.get("limit_expired", 0)) + 1
                log_event(state, f"{symbol}: paper limit entry expired", ctx.timezone)
                return "limit entry expired"
            return f"limit pending at {limit_price:.6g}"

    state["pending_entries"].pop(symbol, None)
    diagnostics = execution_diagnostics(state, ctx.timezone)
    diagnostics["limit_fills"] = int(diagnostics.get("limit_fills", 0)) + 1
    open_position(
        ctx,
        state,
        symbol,
        str(pending["side"]),
        filled_candle,
        float(pending["atr"]),
        str(pending["reason"]),
        entry_override=float(pending["limit_price"]),
        broker_result_override=broker_result,
        trade_profile=dict(pending.get("trade_profile", {})),
    )
    return f"limit filled {pending['side']}"


def apply_exchange_account_summary(
    ctx: BotContext,
    state: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    normalized = [normalize_broker_position(item) for item in summary["positions"]]
    positions = {
        str(item["symbol"]): item
        for item in normalized
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
        "provider": broker_provider(ctx),
        "name": broker_name(ctx),
        "connected": True,
        "orders_enabled": ctx.orders_enabled,
        "environment": summary["environment"],
        "position_mode": summary["position_mode"],
        "available_balance": float(summary["available_balance"]),
        "time_offset_ms": summary.get("time_offset_ms"),
        "time_sync_rtt_ms": summary.get("time_sync_rtt_ms"),
        "message": "Connected",
    }
    state.pop("last_broker_error", None)


def apply_exchange_account_error(
    ctx: BotContext,
    state: dict[str, Any],
    exc: Exception,
) -> None:
    message = str(exc)
    state["broker_status"] = {
        "mode": ctx.mode,
        "provider": broker_provider(ctx),
        "name": broker_name(ctx),
        "connected": False,
        "orders_enabled": ctx.orders_enabled,
        "message": message,
    }
    if state.get("last_broker_error") != message:
        log_event(state, f"{broker_name(ctx)} connection error: {message}", ctx.timezone)
        state["last_broker_error"] = message
    ctx.exchange_snapshot = {}


def refresh_exchange_account(ctx: BotContext, state: dict[str, Any]) -> None:
    if ctx.broker is None:
        state["broker_status"] = {
            "mode": "paper",
            "provider": "paper",
            "name": "Paper",
            "connected": True,
            "orders_enabled": True,
            "message": "Paper simulator",
        }
        return

    try:
        summary = ctx.broker.account_summary()
        apply_exchange_account_summary(ctx, state, summary)
    except Exception as exc:
        apply_exchange_account_error(ctx, state, exc)


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
        "realized_r": position_realized_r(position, pnl),
        "balance": round(float(state["balance"]), 2),
        "reason": (
            f"{broker_name(ctx)} position closed; realized={pnl_info['realized_pnl']:.2f}, "
            f"commission={pnl_info['commission']:.2f}"
        ),
        "source": str(position.get("source", "baseline")),
        "validation_date": validation_trade_date(
            {"time": position.get("opened_at")},
            ctx.timezone,
        ),
    }
    append_trade(ctx, state, row)
    ctx.broker.cancel_protection(symbol)
    log_event(state, f"{symbol}: {broker_name(ctx)} position closed, net pnl={pnl:.2f}", ctx.timezone)
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
        verify_exchange_protection(ctx, state, symbol, position, exchange_position)
        return
    record_exchange_close(ctx, state, symbol, position, candle.close)


def verify_exchange_protection(
    ctx: BotContext,
    state: dict[str, Any],
    symbol: str,
    position: dict[str, Any],
    exchange_position: dict[str, Any],
) -> None:
    if ctx.broker is None:
        return
    position["entry"] = float(exchange_position.get("entry") or position["entry"])
    position["qty"] = abs(float(exchange_position.get("quantity", 0.0)))
    position["side"] = str(exchange_position.get("side") or position["side"])
    position["exchange_unrealized_pnl"] = float(exchange_position.get("unrealized_pnl", 0))
    if (
        not ctx.orders_enabled
        or position.get("emergency_close_sent")
        or position.get("time_exit_sent")
        or ctx.broker.has_stop_and_target(symbol)
    ):
        return
    ctx.broker.cancel_protection(symbol)
    ctx.broker.market_close(symbol, exchange_position)
    position["emergency_close_sent"] = True
    log_event(
        state,
        f"{symbol}: protection missing; emergency {broker_name(ctx)} close sent",
        ctx.timezone,
    )
    notify_safely(
        ctx,
        state,
        f"Crypto Autobot [{ctx.mode.upper()}]\n"
        f"Emergency close sent for {symbol}: Stop Loss or Take Profit was missing.",
    )


def manage_position(ctx: BotContext, state: dict[str, Any], symbol: str, candle: Candle, atr_value: float | None) -> None:
    position = state.get("positions", {}).get(symbol)
    if not position:
        return
    if ctx.broker is not None:
        sync_exchange_position(ctx, state, symbol, candle)
        position = state.get("positions", {}).get(symbol)
        if not position:
            return
        max_holding = int(position.get("max_holding_bars", ctx.config["strategy"].get("max_holding_bars", 0)))
        interval_ms = interval_milliseconds(str(ctx.config["market"]["interval"]))
        bars_held = max(
            0,
            (candle.open_time - int(position.get("opened_candle_time", candle.open_time)))
            // interval_ms,
        )
        if (
            max_holding
            and bars_held >= max_holding
            and ctx.orders_enabled
            and not position.get("time_exit_sent")
            and not position.get("emergency_close_sent")
        ):
            exchange_position = ctx.exchange_snapshot.get(symbol)
            if exchange_position:
                ctx.broker.cancel_protection(symbol)
                ctx.broker.market_close(symbol, exchange_position)
                position["time_exit_sent"] = True
                log_event(
                    state,
                    f"{symbol}: {max_holding}-bar time exit sent to {broker_name(ctx)}",
                    ctx.timezone,
                )
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
            if risk > 0 and candle.high >= entry + risk * float(position.get("trail_after_r", ctx.config["strategy"]["trail_after_r"])):
                position["stop"] = max(stop, candle.close - atr_value * float(position.get("trail_atr", ctx.config["strategy"]["trail_atr"])))
                stop = float(position["stop"])
        else:
            position["lowest"] = min(float(position.get("lowest", candle.low)), candle.low)
            risk = float(position["initial_stop"]) - entry
            if risk > 0 and candle.low <= entry - risk * float(position.get("trail_after_r", ctx.config["strategy"]["trail_after_r"])):
                position["stop"] = min(stop, candle.close + atr_value * float(position.get("trail_atr", ctx.config["strategy"]["trail_atr"])))
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
    else:
        max_holding = int(position.get("max_holding_bars", ctx.config["strategy"].get("max_holding_bars", 0)))
        interval_ms = interval_milliseconds(str(ctx.config["market"]["interval"]))
        bars_held = max(
            0,
            (candle.open_time - int(position.get("opened_candle_time", candle.open_time)))
            // interval_ms,
        )
        if max_holding and bars_held >= max_holding:
            close_position(ctx, state, symbol, candle.close, "time_exit")


def scan_symbol(
    ctx: BotContext,
    state: dict[str, Any],
    symbol: str,
    btc_candles: list[Candle] | None = None,
    candles: list[Candle] | None = None,
) -> dict[str, Any]:
    market = ctx.config["market"]
    strategy = ctx.config["strategy"]
    if candles is None:
        candles = fetch_market_candles(ctx, symbol)
    strategy_type = str(strategy.get("type", "legacy_breakout"))
    if strategy_type in INTRADAY_STRATEGIES:
        min_needed = intraday_minimum_history(strategy)
    else:
        min_needed = max(
            int(strategy["slow_ema"]) + int(strategy.get("slow_slope_lookback", 1)),
            int(strategy["breakout_lookback"]) + 2,
            int(strategy["volume_sma_length"]),
            int(strategy["atr_length"]),
        )
    if len(candles) < min_needed:
        return {
            "symbol": symbol,
            "status": "not enough candles",
            "candles": len(candles),
            "candle_open_time": candles[-1].open_time if candles else None,
        }
    interval = str(market["interval"])
    max_age_intervals = float(market.get("max_candle_age_intervals", 2.0))
    if not market_data_is_fresh(candles[-1], interval, max_age_intervals):
        return {
            "symbol": symbol,
            "time": candles[-1].open_dt,
            "price": candles[-1].close,
            "candle_open_time": candles[-1].open_time,
            "data_source": broker_name(ctx) if broker_provider(ctx) == "mt5" else "Binance",
            "status": "stale market data; trading blocked",
        }

    intraday_indicators = None
    if strategy_type in INTRADAY_STRATEGIES:
        intraday_indicators = build_intraday_indicators(candles, strategy)
        atrs = intraday_indicators.atr
        fast = intraday_indicators.entry_fast
        slow = intraday_indicators.regime_slow
        vol_sma: list[float | None] = []
        adxs: list[float | None] = []
    else:
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
    pending_status = reconcile_pending_entry(ctx, state, symbol, candles)
    manage_position(ctx, state, symbol, candle, atr_value)

    latest = {
        "symbol": symbol,
        "time": candle.open_dt,
        "price": candle.close,
        "candle_open_time": candle.open_time,
        "data_source": broker_name(ctx) if broker_provider(ctx) == "mt5" else "Binance",
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
    if symbol in state.get("pending_entries", {}):
        latest["status"] = pending_status or "limit pending"
        return latest
    if symbol in state.get("exchange_positions", {}):
        latest["status"] = f"external {broker_name(ctx)} position; bot will not modify it"
        return latest

    baseline_symbols = {
        str(item).upper()
        for item in ctx.config.get("ensemble", {}).get("baseline_symbols", market["symbols"])
    }
    side = None
    reason = ""
    signal_status = "outside baseline universe"
    trade_profile: dict[str, Any] | None = None
    signal_source = "baseline"
    if symbol in baseline_symbols:
        if strategy_type in INTRADAY_STRATEGIES:
            decision = evaluate_strategy_signal(candles, i, strategy, intraday_indicators)
            side, reason, signal_status = decision.side, decision.reason, decision.status
        else:
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
    ensemble = ctx.config.get("ensemble", {})
    baseline_status = signal_status
    if not side and ensemble.get("enabled", False):
        reference = btc_candles or (candles if symbol == "BTCUSDT" else [])
        ml_decision = evaluate_orderflow_signal(ensemble, ROOT, symbol, candles, reference)
        signal_status = (
            f"{baseline_status}; ML: {ml_decision.status}"
            if symbol in baseline_symbols
            else ml_decision.status
        )
        if ml_decision.side:
            side = ml_decision.side
            atr_value = ml_decision.atr
            reason = f"orderflow_ml score={ml_decision.score:.5f} threshold={ml_decision.threshold:.5f}"
            signal_source = "orderflow_ml"
            trade_profile = {
                "source": signal_source,
                "entry_order_type": "market",
                "target_order_type": str(ensemble.get("target_order_type", "limit")),
                "stop_atr": float(ensemble.get("stop_atr", 1.5)),
                "target_atr": float(ensemble.get("target_atr", 3.0)),
                "max_holding_bars": int(ensemble.get("max_holding_bars", 16)),
                "trail_after_r": float(ensemble.get("trail_after_r", 99.0)),
                "trail_atr": float(ensemble.get("trail_atr", 1.5)),
            }
    if signal_source == "orderflow_ml":
        ml_open = sum(
            position.get("source") == "orderflow_ml"
            for position in state.get("positions", {}).values()
        ) + sum(
            pending.get("trade_profile", {}).get("source") == "orderflow_ml"
            for pending in state.get("pending_entries", {}).values()
        )
        if ml_open >= int(ensemble.get("max_open_positions", 3)):
            latest["status"] = "ML position limit reached"
            return latest
        if int(daily_stats(state, ctx).get("orderflow_ml_trades", 0)) >= int(
            ensemble.get("max_daily_trades", 6)
        ):
            latest["status"] = "ML daily trade limit reached"
            return latest
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
    entry_order_type = str((trade_profile or strategy).get("entry_order_type", "market"))
    if entry_order_type == "limit_retrace":
        place_pending_entry(ctx, state, symbol, side, candle, atr_value, reason, trade_profile)
        latest["status"] = f"placed {side} limit"
    else:
        open_position(ctx, state, symbol, side, candle, atr_value, reason, trade_profile=trade_profile)
        if symbol in state.get("positions", {}):
            diagnostics = execution_diagnostics(state, ctx.timezone)
            diagnostics["market_entries"] = int(diagnostics.get("market_entries", 0)) + 1
            latest["status"] = f"opened {side}"
        else:
            latest["status"] = "market entry rejected"
    latest["signal_source"] = signal_source
    state["seen_signal_candles"][symbol] = seen_key

    return latest


def fetch_scan_candles(
    ctx: BotContext,
    symbols: list[str],
) -> tuple[dict[str, list[Candle]], dict[str, Exception]]:
    worker_count = max(1, int(ctx.config.get("app", {}).get("market_fetch_workers", 1)))
    if broker_provider(ctx) == "mt5":
        worker_count = 1
    worker_count = min(worker_count, len(symbols)) if symbols else 1
    candles_by_symbol: dict[str, list[Candle]] = {}
    errors: dict[str, Exception] = {}

    if worker_count == 1:
        for symbol in symbols:
            try:
                candles_by_symbol[symbol] = fetch_market_candles(ctx, symbol)
            except Exception as exc:  # noqa: BLE001
                errors[symbol] = exc
        return candles_by_symbol, errors

    # Only public Binance candle requests run concurrently. State mutation,
    # signal evaluation and every broker order remain serialized below.
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="market-data") as pool:
        futures = {
            symbol: pool.submit(fetch_market_candles, ctx, symbol)
            for symbol in symbols
        }
        for symbol in symbols:
            try:
                candles_by_symbol[symbol] = futures[symbol].result()
            except Exception as exc:  # noqa: BLE001
                errors[symbol] = exc
    return candles_by_symbol, errors


def scan_once(ctx: BotContext) -> dict[str, Any]:
    started_monotonic = time.monotonic()
    with ctx.lock:
        ensure_trades_file(ctx)
        state = ensure_state(ctx)
        runtime = state.setdefault("runtime", {})
        runtime["scan_sequence"] = int(runtime.get("scan_sequence", 0)) + 1
        runtime["scan_in_progress"] = True
        runtime["last_scan_started_at"] = now_iso(ctx.timezone)
        state.setdefault("validation_coverage", {})
        write_state(ctx, state)

    account_summary: dict[str, Any] | None = None
    account_error: Exception | None = None
    if ctx.broker is not None:
        try:
            account_summary = ctx.broker.account_summary()
        except Exception as exc:  # noqa: BLE001
            account_error = exc

    with ctx.lock:
        state = ensure_state(ctx)
        if ctx.broker is None:
            refresh_exchange_account(ctx, state)
        elif account_error is not None:
            apply_exchange_account_error(ctx, state, account_error)
        elif account_summary is not None:
            apply_exchange_account_summary(ctx, state, account_summary)
        write_state(ctx, state)

    results: list[dict[str, Any]] = []
    error_count = 0
    btc_candles: list[Candle] | None = None
    ml_runtime_status = orderflow_model_status(ctx.config.get("ensemble", {}), ROOT)
    with ctx.lock:
        state = ensure_state(ctx)
        runtime = state.setdefault("runtime", {})
        runtime["ensemble"] = ml_runtime_status
        write_state(ctx, state)

    symbols = [str(item).upper() for item in ctx.config["market"]["symbols"]]
    candles_by_symbol, fetch_errors = fetch_scan_candles(ctx, symbols)

    if ml_runtime_status.get("ready", False):
        btc_candles = candles_by_symbol.get("BTCUSDT")
        if btc_candles is None:
            try:
                btc_candles = fetch_market_candles(ctx, "BTCUSDT")
            except Exception as exc:  # noqa: BLE001
                with ctx.lock:
                    state = ensure_state(ctx)
                    log_event(state, f"BTCUSDT: ML reference error: {exc}", ctx.timezone)
                    write_state(ctx, state)

    for symbol in symbols:
        try:
            if symbol in fetch_errors:
                raise fetch_errors[symbol]
            candles = candles_by_symbol[symbol]
            with ctx.lock:
                state = ensure_state(ctx)
                result = scan_symbol(
                    ctx,
                    state,
                    symbol,
                    btc_candles,
                    candles,
                )
                results.append(result)
                record_scan_diagnostic(state, result, ctx.timezone)
                state.setdefault("latest", {})[symbol] = result
                write_state(ctx, state)
        except Exception as exc:  # noqa: BLE001
            error_count += 1
            message = f"{symbol}: scan error: {exc}"
            results.append({"symbol": symbol, "status": message})
            with ctx.lock:
                state = ensure_state(ctx)
                log_event(state, message, ctx.timezone)
                write_state(ctx, state)

    with ctx.lock:
        state = ensure_state(ctx)
        runtime = state.setdefault("runtime", {})
        runtime["scan_in_progress"] = False
        runtime["last_scan_completed_at"] = now_iso(ctx.timezone)
        runtime["last_scan_duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
        runtime["last_scan_symbol_errors"] = error_count
        all_failed = bool(results) and error_count == len(results)
        runtime["consecutive_failures"] = (
            int(runtime.get("consecutive_failures", 0)) + 1 if all_failed else 0
        )
        write_state(ctx, state)
        return {"status": "ok", "results": results, "state": public_state(ctx, state)}


def open_demo_test_order(
    ctx: BotContext,
    symbol: str,
    side: str,
    confirmation: str,
) -> dict[str, Any]:
    if ctx.mode != "demo" or broker_provider(ctx) != "binance":
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
        if (
            symbol in state.get("positions", {})
            or symbol in state.get("pending_entries", {})
            or symbol in state.get("exchange_positions", {})
        ):
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
            trade_profile={"source": "manual_demo_test"},
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
                runtime = state.setdefault("runtime", {})
                runtime["scan_in_progress"] = False
                runtime["last_scan_failed_at"] = now_iso(ctx.timezone)
                runtime["last_scan_error"] = str(exc)
                runtime["consecutive_failures"] = int(runtime.get("consecutive_failures", 0)) + 1
                log_event(state, f"worker error: {exc}", ctx.timezone)
                write_state(ctx, state)
        interval = int(ctx.config["app"]["scan_interval_seconds"])
        controller.wake_event.wait(interval)
        controller.wake_event.clear()


def broker_watchdog_once(ctx: BotContext) -> dict[str, Any]:
    if ctx.broker is None or not ctx.orders_enabled:
        return {"status": "disabled", "checked_pending": 0, "checked_positions": 0}
    with ctx.lock:
        ensure_trades_file(ctx)
        state = ensure_state(ctx)
        runtime = state.setdefault("runtime", {})
        runtime["watchdog_in_progress"] = True
        runtime["last_watchdog_started_at"] = now_iso(ctx.timezone)
        write_state(ctx, state)

    try:
        normalized = [
            normalize_broker_position(item)
            for item in ctx.broker.get_open_positions()
        ]
        exchange_positions = {
            str(item["symbol"]): item
            for item in normalized
            if str(item.get("symbol", "")).upper()
        }
    except Exception as exc:  # noqa: BLE001
        with ctx.lock:
            state = ensure_state(ctx)
            apply_exchange_account_error(ctx, state, exc)
            runtime = state.setdefault("runtime", {})
            runtime["watchdog_in_progress"] = False
            runtime["last_watchdog_completed_at"] = now_iso(ctx.timezone)
            runtime["last_watchdog_errors"] = 1
            runtime["last_watchdog_error"] = str(exc)
            write_state(ctx, state)
        return {
            "status": "degraded",
            "checked_pending": 0,
            "checked_positions": 0,
            "errors": 1,
        }

    with ctx.lock:
        state = ensure_state(ctx)
        ctx.exchange_snapshot = exchange_positions
        state["exchange_positions"] = exchange_positions
        broker_status = state.setdefault("broker_status", {})
        broker_status.update({
            "mode": ctx.mode,
            "provider": broker_provider(ctx),
            "name": broker_name(ctx),
            "connected": True,
            "orders_enabled": ctx.orders_enabled,
            "message": "Connected",
        })
        state.pop("last_broker_error", None)
        runtime = state.setdefault("runtime", {})
        checked_pending = 0
        checked_positions = 0
        errors = 0
        if state.get("broker_status", {}).get("connected"):
            interval_ms = interval_milliseconds(str(ctx.config["market"]["interval"]))
            current_open = int(time.time() * 1000) // interval_ms * interval_ms
            for symbol in list(state.get("pending_entries", {})):
                pending = state.get("pending_entries", {}).get(symbol)
                if not pending:
                    continue
                checked_pending += 1
                price = float(pending["limit_price"])
                candle = Candle(
                    open_time=current_open,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=0.0,
                    close_time=current_open + interval_ms - 1,
                )
                try:
                    reconcile_pending_entry(ctx, state, symbol, [candle])
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    message = f"{symbol}: protection watchdog entry error: {exc}"
                    if state.get("last_watchdog_entry_error", {}).get(symbol) != str(exc):
                        log_event(state, message, ctx.timezone)
                        notify_safely(
                            ctx,
                            state,
                            f"Crypto Autobot [{ctx.mode.upper()}]\n"
                            f"Protection watchdog could not process filled entry {symbol}: {exc}",
                        )
                    state.setdefault("last_watchdog_entry_error", {})[symbol] = str(exc)
            for symbol, position in list(state.get("positions", {}).items()):
                exchange_position = ctx.exchange_snapshot.get(symbol)
                if not exchange_position:
                    continue
                checked_positions += 1
                try:
                    verify_exchange_protection(ctx, state, symbol, position, exchange_position)
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    message = f"{symbol}: protection watchdog position error: {exc}"
                    if state.get("last_watchdog_position_error", {}).get(symbol) != str(exc):
                        log_event(state, message, ctx.timezone)
                        notify_safely(
                            ctx,
                            state,
                            f"Crypto Autobot [{ctx.mode.upper()}]\n"
                            f"Protection watchdog failed for open position {symbol}: {exc}",
                        )
                    state.setdefault("last_watchdog_position_error", {})[symbol] = str(exc)
        runtime["watchdog_in_progress"] = False
        runtime["last_watchdog_completed_at"] = now_iso(ctx.timezone)
        runtime["last_watchdog_errors"] = errors
        write_state(ctx, state)
        return {
            "status": "ok" if errors == 0 else "degraded",
            "checked_pending": checked_pending,
            "checked_positions": checked_positions,
            "errors": errors,
        }


def protection_watchdog_loop(controller: RuntimeController) -> None:
    while not controller.stop_event.is_set():
        ctx = controller.current()
        try:
            broker_watchdog_once(ctx)
        except Exception as exc:  # noqa: BLE001
            with ctx.lock:
                state = ensure_state(ctx)
                runtime = state.setdefault("runtime", {})
                runtime["watchdog_in_progress"] = False
                runtime["last_watchdog_failed_at"] = now_iso(ctx.timezone)
                runtime["last_watchdog_error"] = str(exc)
                log_event(state, f"protection watchdog error: {exc}", ctx.timezone)
                write_state(ctx, state)
        seconds = max(1, int(ctx.config["app"].get("protection_watchdog_seconds", 2)))
        controller.stop_event.wait(seconds)


def broker_readiness_snapshot(ctx: BotContext) -> dict[str, Any]:
    if ctx.broker is None:
        return {
            "status": "ok",
            "ready": True,
            "mode": "paper",
            "message": "No broker credentials required.",
            "orders_sent": 0,
        }

    summary = ctx.broker.account_summary()
    strategy = ctx.config.get("strategy", {})
    strategy_type = str(strategy.get("type", "legacy_breakout"))
    if strategy_type in INTRADAY_STRATEGIES:
        minimum = intraday_minimum_history(strategy)
    else:
        minimum = max(
            int(strategy.get("atr_length", 14)) + 2,
            int(strategy.get("slow_ema", 0)) + 2,
        )
    interval = str(ctx.config["market"]["interval"])
    max_age = float(ctx.config["market"].get("max_candle_age_intervals", 2.0))
    configured_symbols = [
        str(symbol).upper() for symbol in ctx.config["market"]["symbols"]
    ]
    provider_checks: dict[str, Any] = {}
    provider_readiness = getattr(ctx.broker, "readiness_snapshot", None)
    if callable(provider_readiness):
        provider_checks = provider_readiness(configured_symbols)
    symbols: list[dict[str, Any]] = []
    ready = (
        float(summary.get("available_balance", 0.0)) > 0
        and bool(provider_checks.get("ready", True))
    )
    for symbol in configured_symbols:
        item: dict[str, Any] = {"symbol": symbol, "ready": False}
        try:
            candles = fetch_market_candles(ctx, symbol)
            enough_history = len(candles) >= minimum
            fresh = bool(candles) and market_data_is_fresh(
                candles[-1], interval, max_age
            )
            if broker_provider(ctx) == "binance":
                rules_getter = getattr(ctx.broker, "symbol_rules", None)
                if callable(rules_getter):
                    rules_getter(symbol)
            item.update(
                {
                    "ready": enough_history and fresh,
                    "closed_candles": len(candles),
                    "minimum_candles": minimum,
                    "fresh": fresh,
                    "last_candle": candles[-1].open_dt if candles else None,
                    "data_source": broker_name(ctx),
                }
            )
        except Exception as exc:  # noqa: BLE001
            item["error"] = str(exc)
        ready = ready and bool(item["ready"])
        symbols.append(item)
    return {
        "status": "ok" if ready else "not_ready",
        "ready": ready,
        "mode": ctx.mode,
        "broker": broker_name(ctx),
        "environment": summary.get("environment"),
        "asset": summary.get("asset"),
        "balance": float(summary.get("balance", 0.0)),
        "available_balance": float(summary.get("available_balance", 0.0)),
        "open_positions": len(summary.get("positions", [])),
        "position_mode": summary.get("position_mode"),
        "orders_enabled": summary.get("orders_enabled"),
        "orders_sent": 0,
        "broker_checks": provider_checks,
        "symbols": symbols,
    }


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
        "pending_entries": len(state.get("pending_entries", {})),
    }


def public_state(ctx: BotContext, state: dict[str, Any]) -> dict[str, Any]:
    prices = {symbol: data.get("price") for symbol, data in state.get("latest", {}).items()}
    active_symbols = set(ctx.config.get("market", {}).get("symbols", []))
    active_latest = {
        symbol: data
        for symbol, data in state.get("latest", {}).items()
        if symbol in active_symbols
    }
    strategy = ctx.config.get("strategy", {})
    stop_atr = float(strategy.get("stop_atr", 0.0))
    target_atr = float(strategy.get("target_atr", 0.0))
    max_holding_bars = int(strategy.get("max_holding_bars", 0))
    interval = str(ctx.config.get("market", {}).get("interval", "15m"))
    max_holding_hours = (
        max_holding_bars * interval_milliseconds(interval) / 3_600_000
        if max_holding_bars
        else 0.0
    )
    ensemble_config = ctx.config.get("ensemble", {})
    ensemble_status = orderflow_model_status(ensemble_config, ROOT)
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
        displayed_positions[symbol] = {
            "symbol": symbol,
            "side": str(exchange_position.get("side", "")),
            "entry": float(exchange_position.get("entry", 0)),
            "qty": abs(float(exchange_position.get("quantity", 0))),
            "stop": float(exchange_position.get("stop", 0)) or None,
            "target": float(exchange_position.get("target", 0)) or None,
            "unrealized_pnl": round(float(exchange_position.get("unrealized_pnl", 0)), 2),
            "external": True,
        }
        open_pnl += float(exchange_position.get("unrealized_pnl", 0))

    stats = stats_from_state(state)
    stats["open_positions"] = len(displayed_positions)
    diagnostics = dict(execution_diagnostics(state, ctx.timezone))
    signal_orders = int(diagnostics.get("signal_orders", 0))
    diagnostics["fill_rate_percent"] = round(
        int(diagnostics.get("limit_fills", 0)) / signal_orders * 100,
        2,
    ) if signal_orders else 0.0
    diagnostics.pop("last_candle_by_symbol", None)
    result = {
        "updated_at": state.get("updated_at"),
        "mode": ctx.mode,
        "broker_provider": broker_provider(ctx),
        "broker_name": broker_name(ctx) if ctx.broker is not None else "Paper",
        "orders_enabled": ctx.orders_enabled,
        "runtime": state.get("runtime", {}),
        "strategy_profile": {
            "timeframe": interval,
            "reward_risk": round(target_atr / stop_atr, 2) if stop_atr else 0.0,
            "target_order_type": str(strategy.get("target_order_type", "market")),
            "max_holding_hours": round(max_holding_hours, 2),
            "risk_per_trade_percent": float(ctx.config.get("account", {}).get("risk_per_trade_percent", 0.0)),
            "long_risk_per_trade_percent": side_risk_percent(ctx.config.get("account", {}), "long"),
            "short_risk_per_trade_percent": side_risk_percent(ctx.config.get("account", {}), "short"),
            "status": "PAPER VALIDATION" if ctx.mode == "paper" else "DEMO CANDIDATE",
            "ensemble_enabled": bool(ensemble_config.get("enabled", False)),
            "ensemble_reward_risk": round(
                float(ensemble_config.get("target_atr", 0.0))
                / float(ensemble_config.get("stop_atr", 1.0)),
                2,
            ) if ensemble_config.get("enabled", False) else None,
        },
        "ensemble_status": ensemble_status,
        "market": {
            "symbols": list(ctx.config.get("market", {}).get("symbols", [])),
            "interval": ctx.config.get("market", {}).get("interval"),
            "base_url": ctx.config.get("market", {}).get("base_url"),
            "data_environment": market_data_environment(ctx),
        },
        "broker_status": state.get("broker_status", {}),
        "health": health_snapshot(ctx, state)[0],
        "stats": stats,
        "execution_diagnostics": diagnostics,
        "equity_now": round(float(state.get("balance", 0.0)) + open_pnl, 2),
        "positions": displayed_positions,
        "pending_entries": state.get("pending_entries", {}),
        "latest": active_latest,
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


def health_snapshot(
    ctx: BotContext,
    state: dict[str, Any],
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], int]:
    broker_status = state.get("broker_status", {})
    runtime = state.get("runtime", {})
    scan_interval = int(ctx.config["app"].get("scan_interval_seconds", 60))
    stale_after = int(
        ctx.config["app"].get("health_stale_after_seconds", max(180, scan_interval * 6))
    )
    heartbeat_text = (
        runtime.get("last_scan_started_at")
        if runtime.get("scan_in_progress")
        else runtime.get("last_scan_completed_at") or runtime.get("last_scan_started_at")
    )
    heartbeat_age: float | None = None
    if heartbeat_text:
        try:
            heartbeat = dt.datetime.fromisoformat(str(heartbeat_text))
            current = now or dt.datetime.now(heartbeat.tzinfo or dt.timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=heartbeat.tzinfo or dt.timezone.utc)
            heartbeat_age = max(0.0, (current - heartbeat).total_seconds())
        except ValueError:
            heartbeat_age = None
    stale = heartbeat_age is None or heartbeat_age > stale_after
    watchdog_required = ctx.broker is not None and ctx.orders_enabled
    watchdog_age: float | None = None
    watchdog_text = (
        runtime.get("last_watchdog_started_at")
        if runtime.get("watchdog_in_progress")
        else runtime.get("last_watchdog_completed_at") or runtime.get("last_watchdog_started_at")
    )
    if watchdog_text:
        try:
            watchdog_time = dt.datetime.fromisoformat(str(watchdog_text))
            current = now or dt.datetime.now(watchdog_time.tzinfo or dt.timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=watchdog_time.tzinfo or dt.timezone.utc)
            watchdog_age = max(0.0, (current - watchdog_time).total_seconds())
        except ValueError:
            watchdog_age = None
    watchdog_stale_after = max(
        10,
        int(ctx.config["app"].get("protection_watchdog_seconds", 2)) * 5,
    )
    watchdog_stale = watchdog_required and (
        watchdog_age is None or watchdog_age > watchdog_stale_after
    )
    broker_connected = bool(broker_status.get("connected", ctx.mode == "paper"))
    healthy = not stale and not watchdog_stale and (ctx.mode == "paper" or broker_connected)
    recovery = state.get("state_recovery", {})
    payload = {
        "status": "ok" if healthy else "degraded",
        "mode": ctx.mode,
        "broker_provider": broker_provider(ctx),
        "broker_name": broker_name(ctx) if ctx.broker is not None else "Paper",
        "broker_connected": broker_connected,
        "binance_connected": broker_connected,
        "orders_enabled": ctx.orders_enabled,
        "heartbeat_age_seconds": round(heartbeat_age, 1) if heartbeat_age is not None else None,
        "stale_after_seconds": stale_after,
        "scan_in_progress": bool(runtime.get("scan_in_progress")),
        "last_scan_duration_seconds": runtime.get("last_scan_duration_seconds"),
        "last_scan_symbol_errors": runtime.get("last_scan_symbol_errors"),
        "consecutive_failures": int(runtime.get("consecutive_failures", 0)),
        "watchdog_required": watchdog_required,
        "watchdog_age_seconds": round(watchdog_age, 1) if watchdog_age is not None else None,
        "watchdog_stale_after_seconds": watchdog_stale_after,
        "last_watchdog_errors": runtime.get("last_watchdog_errors"),
        "state_backup_generations": sum(
            backup.exists() for backup in state_backup_paths(ctx.state_path)
        ),
        "state_recovered_at": recovery.get("at"),
        "state_recovery_source": recovery.get("source"),
    }
    return payload, 200 if healthy else 503


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
      grid-template-columns: repeat(4, minmax(150px, 1fr));
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
        <span class="badge" id="marketBadge">Данные: -</span>
        <span class="badge" id="ordersBadge">Ордера: -</span>
        <span class="badge" id="heartbeatBadge">Цикл: -</span>
        <span class="badge" id="watchdogBadge">Защита: -</span>
        <span class="badge" id="stateBadge">State: -</span>
        <span class="badge" id="mlBadge">ML: -</span>
        <span class="badge" id="validationBadge">Live gate: -</span>
      </div>
      <div class="muted" id="updated">Загрузка...</div>
      <div class="muted" id="profileInfo"></div>
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
    <div class="card"><div class="label">Profit factor</div><div class="value" id="profitFactor">-</div></div>
    <div class="card"><div class="label">Сделок в день</div><div class="value" id="tradesPerDay">-</div></div>
    <div class="card"><div class="label">Открытые позиции</div><div class="value" id="openpos">-</div></div>
    <div class="card"><div class="label">Лимитные заявки</div><div class="value" id="pending">-</div></div>
    <div class="card"><div class="label">Макс. Demo-просадка</div><div class="value" id="maxDrawdown">-</div></div>
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
        <h2>Готовность к Live</h2>
        <div class="log" id="validationSummary">Собираем данные...</div>
        <div class="tablewrap"><table>
          <thead><tr><th>Критерий</th><th>Сейчас</th><th>Нужно</th><th>Статус</th></tr></thead>
          <tbody id="validationRows"></tbody>
        </table></div>
      </section>
      <section>
        <h2>Надёжность выборки</h2>
        <div class="log" id="confidenceSummary">Собираем статистику...</div>
        <div class="tablewrap"><table>
          <thead><tr><th>Метрика</th><th>Оценка</th><th>Что означает</th></tr></thead>
          <tbody id="confidenceRows"></tbody>
        </table></div>
      </section>
      <section>
        <h2>Исполнение сигналов</h2>
        <div class="tablewrap"><table>
          <thead><tr><th>Закрытых свечей</th><th>Заявок</th><th>Исполнено</th><th>Истекло</th><th>Отменено</th><th>Fill rate</th></tr></thead>
          <tbody id="executionRows"></tbody>
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
  const profile = data.strategy_profile || {{}};
  document.getElementById('profileInfo').textContent =
    `Профиль: ${{profile.timeframe || '-'}} · RR 1:${{profile.reward_risk || '-'}} · ` +
    `${{profile.ensemble_enabled ? `ML RR 1:${{profile.ensemble_reward_risk || '-'}} · ` : ''}}` +
    `до ${{profile.max_holding_hours || '-'}} ч · тейк ${{String(profile.target_order_type || '-').toUpperCase()}} · ` +
    `риск S/L ${{profile.short_risk_per_trade_percent ?? profile.risk_per_trade_percent ?? 0}}%/` +
    `${{profile.long_risk_per_trade_percent ?? profile.risk_per_trade_percent ?? 0}}% · ${{profile.status || ''}}`;
  const connected = Boolean(data.broker_status?.connected);
  document.getElementById('modeBadge').textContent = `Режим: ${{String(data.mode || '-').toUpperCase()}}`;
  document.getElementById('modeBadge').className = `badge ${{data.mode === 'live' ? 'danger' : (data.mode === 'demo' ? 'warn' : 'ok')}}`;
  document.getElementById('brokerBadge').textContent = `${{data.broker_name || 'Broker'}}: ${{connected ? 'подключён' : 'нет подключения'}}`;
  document.getElementById('brokerBadge').className = `badge ${{connected ? 'ok' : 'danger'}}`;
  document.getElementById('brokerBadge').title = connected && data.broker_status?.time_sync_rtt_ms !== null && data.broker_status?.time_sync_rtt_ms !== undefined
    ? `Синхронизация времени: RTT ${{data.broker_status.time_sync_rtt_ms}} мс, offset ${{data.broker_status.time_offset_ms ?? '-'}} мс`
    : (data.broker_status?.message || '');
  document.getElementById('marketBadge').textContent = `Данные: ${{data.market?.data_environment || '-'}}`;
  document.getElementById('marketBadge').className = 'badge ok';
  document.getElementById('marketBadge').title = data.market?.base_url || '';
  document.getElementById('ordersBadge').textContent = `Ордера: ${{data.orders_enabled ? 'разрешены' : 'заблокированы'}}`;
  document.getElementById('ordersBadge').className = `badge ${{data.orders_enabled ? (data.mode === 'live' ? 'danger' : 'warn') : 'ok'}}`;
  const health = data.health || {{}};
  const sequence = Number(data.runtime?.scan_sequence || 0);
  document.getElementById('heartbeatBadge').textContent =
    `Цикл #${{sequence}}: ${{health.status === 'ok' ? 'OK' : 'НЕТ СВЕЖИХ ДАННЫХ'}}`;
  document.getElementById('heartbeatBadge').className = `badge ${{health.status === 'ok' ? 'ok' : 'danger'}}`;
  const watchdogRequired = Boolean(health.watchdog_required);
  const watchdogAge = health.watchdog_age_seconds;
  const watchdogOk = !watchdogRequired || (
    watchdogAge !== null && watchdogAge !== undefined &&
    Number(watchdogAge) <= Number(health.watchdog_stale_after_seconds || 10) &&
    Number(health.last_watchdog_errors || 0) === 0
  );
  document.getElementById('watchdogBadge').textContent = watchdogRequired
    ? `Защита: ${{watchdogOk ? 'активна' : 'ОШИБКА'}}`
    : 'Защита: Paper';
  document.getElementById('watchdogBadge').className = `badge ${{watchdogOk ? 'ok' : 'danger'}}`;
  document.getElementById('watchdogBadge').title = watchdogRequired
    ? `Последняя проверка ${{watchdogAge ?? '-'}} сек. назад`
    : 'Биржевые защитные ордера не используются в Paper';
  const backupGenerations = Number(health.state_backup_generations || 0);
  const stateRecovered = Boolean(health.state_recovered_at);
  document.getElementById('stateBadge').textContent = stateRecovered
    ? 'State: восстановлен'
    : `State: ${{backupGenerations}}/2 копии`;
  document.getElementById('stateBadge').className = `badge ${{stateRecovered ? 'warn' : (backupGenerations > 0 ? 'ok' : 'danger')}}`;
  document.getElementById('stateBadge').title = stateRecovered
    ? `Восстановлено ${{health.state_recovered_at}} из ${{health.state_recovery_source || 'резервной копии'}}`
    : 'Ротационные резервные копии торгового состояния';
  const ml = data.ensemble_status || {{}};
  document.getElementById('mlBadge').textContent = !ml.enabled
    ? 'ML: выключен'
    : `ML: ${{ml.ready ? 'активен' : 'пауза'}}`;
  document.getElementById('mlBadge').className = `badge ${{ml.ready ? 'ok' : (ml.enabled ? 'warn' : '')}}`;
  document.getElementById('mlBadge').title = ml.message || '';
  const validation = data.mode_control?.forward_validation || {{}};
  const validationReady = Boolean(validation.ready_for_live);
  const validationStatus = validation.status || 'collecting';
  document.getElementById('validationBadge').textContent = validationReady
    ? 'Live gate: ПРОЙДЕН'
    : (validationStatus === 'failed' ? 'Live gate: НЕ ПРОЙДЕН' : 'Live gate: СБОР ДАННЫХ');
  document.getElementById('validationBadge').className = `badge ${{validationReady ? 'ok' : (validationStatus === 'failed' ? 'danger' : 'warn')}}`;
  document.getElementById('validationBadge').title = validation.summary || '';
  const demoTestBar = document.getElementById('demoTestBar');
  demoTestBar.hidden = !(data.mode === 'demo' && data.broker_provider === 'binance' && connected && data.orders_enabled);
  const symbolSelect = document.getElementById('demoTestSymbol');
  const symbols = data.market?.symbols || [];
  const selectedSymbol = symbolSelect.value;
  symbolSelect.innerHTML = symbols.map(symbol => `<option value="${{esc(symbol)}}">${{esc(symbol)}}</option>`).join('');
  if (symbols.includes(selectedSymbol)) symbolSelect.value = selectedSymbol;
  document.getElementById('equity').textContent = `$${{money(data.equity_now)}}`;
  document.getElementById('pnl').textContent = `$${{money(data.stats.realized_pnl)}}`;
  document.getElementById('pnl').className = `value ${{cls(data.stats.realized_pnl)}}`;
  document.getElementById('winrate').textContent = `${{money(data.stats.win_rate)}}%`;
  document.getElementById('profitFactor').textContent = validation.profit_factor_infinite
    ? '∞'
    : (validation.profit_factor === null || validation.profit_factor === undefined ? '-' : money(validation.profit_factor));
  document.getElementById('tradesPerDay').textContent = money(validation.trades_per_day);
  document.getElementById('openpos').textContent = data.stats.open_positions;
  document.getElementById('pending').textContent = data.stats.pending_entries;
  document.getElementById('maxDrawdown').textContent = `${{money(validation.max_drawdown_percent)}}%`;

  document.getElementById('validationSummary').textContent = validation.summary || 'Demo-выборка ещё не создана.';
  const validationChecks = validation.checks || [];
  document.getElementById('validationRows').innerHTML = validationChecks.length
    ? validationChecks.map(item => `<tr><td>${{esc(item.label)}}</td><td>${{esc(item.display_value || item.value)}}</td><td>${{esc(item.target)}}</td><td class="${{item.passed ? 'green' : 'red'}}">${{item.passed ? 'OK' : 'ЖДЁМ'}}</td></tr>`).join('')
    : emptyRow(4, 'Demo-метрики недоступны');

  const confidence = validation.confidence || {{}};
  const interval = (item, suffix = '') => item?.lower === null || item?.lower === undefined
    ? '-'
    : `${{num(item.lower)}}..${{num(item.upper)}}${{suffix}}`;
  document.getElementById('confidenceSummary').textContent =
    `Прогресс доказательной выборки: ${{money(confidence.sample_progress_percent)}}%. ` +
    `Положительный expectancy: ${{confidence.positive_expectancy_supported ? 'подтверждён выборкой' : 'ещё не подтверждён'}}. ` +
    `Интервалы приблизительные и не гарантируют будущую прибыль.`;
  const meanR = confidence.mean_realized_r_95 || {{}};
  const payoff = confidence.payoff_ratio === null || confidence.payoff_ratio === undefined
    ? '-'
    : `1:${{num(confidence.payoff_ratio)}}; BE WR ${{num(confidence.break_even_win_rate_percent)}}%`;
  document.getElementById('confidenceRows').innerHTML = [
    ['Win rate, 95%', interval(confidence.win_rate_95_percent, '%'), 'Диапазон правдоподобных значений WR'],
    ['Expectancy/сделку, ~95%', interval(confidence.mean_pnl_95, ' USDT'), 'Нижняя граница должна быть выше нуля'],
    ['Средний результат, R', interval(meanR, 'R'), `Покрытие: ${{confidence.realized_r_coverage || 0}} сделок`],
    ['Частота/день, ~95%', interval(confidence.trades_per_day_95), 'Разброс между подтверждёнными днями'],
    ['Payoff / break-even', payoff, `Запас WR: ${{num(confidence.edge_over_break_even_points)}} п.п.`],
  ].map(item => `<tr><td>${{esc(item[0])}}</td><td>${{esc(item[1])}}</td><td>${{esc(item[2])}}</td></tr>`).join('');

  const execution = data.execution_diagnostics || {{}};
  document.getElementById('executionRows').innerHTML = `<tr>` +
    `<td>${{money(execution.candles_observed)}}</td>` +
    `<td>${{money(execution.signal_orders)}}</td>` +
    `<td>${{money(execution.limit_fills)}}</td>` +
    `<td>${{money(execution.limit_expired)}}</td>` +
    `<td>${{money(execution.limit_canceled)}}</td>` +
    `<td>${{money(execution.fill_rate_percent)}}%</td></tr>`;

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
    btn.disabled = switchingMode || mode === data.mode || !control.control_available || !option.available;
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
                try:
                    state = _load_state_json(ctx.state_path)
                except (FileNotFoundError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                    try:
                        with ctx.lock:
                            state = ensure_state(ctx)
                    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                        state = {}
                payload, status = health_snapshot(ctx, state)
                send_json(self, payload, status=status)
                return
            if self.path == "/api/config":
                safe_config = json.loads(json.dumps(ctx.config))
                send_json(self, safe_config)
                return
            if self.path == "/api/state":
                try:
                    state = json.loads(ctx.state_path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
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
    provider = str(broker_config.get("provider", "binance")).lower()
    if provider not in ("binance", "mt5"):
        raise ValueError("broker.provider must be binance or mt5.")

    broker: BrokerAdapter | None = None
    effective_orders_enabled = True
    if mode != "paper" and provider == "binance":
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
            target_order_type=str(config.get("strategy", {}).get("target_order_type", "market")),
        )
        config.setdefault("market", {}).setdefault("base_url", broker.base_url)
        effective_orders_enabled = orders_enabled
    elif mode != "paper":
        login_env = str(broker_config.get("login_env", "MT5_LOGIN"))
        password_env = str(broker_config.get("password_env", "MT5_PASSWORD"))
        server_env = str(broker_config.get("server_env", "MT5_SERVER"))
        terminal_path_env = str(broker_config.get("terminal_path_env", "MT5_TERMINAL_PATH"))
        login_text = os.environ.get(login_env, "").strip()
        broker = MT5Broker(
            environment=mode,
            orders_enabled=orders_enabled,
            login=int(login_text) if login_text else None,
            password=os.environ.get(password_env, ""),
            server=os.environ.get(server_env, ""),
            terminal_path=os.environ.get(terminal_path_env, "") or None,
            live_confirmation=live_confirmation,
            symbol_map={
                str(key).upper(): str(value)
                for key, value in dict(broker_config.get("symbol_map", {})).items()
            },
            magic=int(broker_config.get("magic", 260802)),
            deviation_points=int(broker_config.get("deviation_points", 20)),
        )
        effective_orders_enabled = orders_enabled

    state_prefix = "" if provider == "binance" else f"{provider}_"
    state_name = "state.json" if mode == "paper" else f"state_{state_prefix}{mode}.json"
    trades_name = "trades.csv" if mode == "paper" else f"trades_{state_prefix}{mode}.csv"
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
        profile_files = dict(self.PROFILE_FILES)
        if ".regime-scalp." in config_path.name:
            profile_files = {
                "paper": "config.paper.regime-scalp.example.json",
                "demo": "config.demo.regime-scalp.example.json",
                "live": "config.live.regime-scalp.example.json",
            }
        if ".ensemble-15m." in config_path.name:
            profile_files = {
                "paper": "config.paper.ensemble-15m.example.json",
                "demo": "config.demo.ensemble-15m.example.json",
                "live": "config.live.ensemble-15m.example.json",
            }
        if ".asymmetric-15m." in config_path.name:
            profile_files = {
                "paper": "config.paper.asymmetric-15m.example.json",
                "demo": "config.demo.asymmetric-15m.example.json",
                "live": "config.live.asymmetric-15m.example.json",
            }
        self.profile_paths = {mode: config_path.parent / filename for mode, filename in profile_files.items()}
        self.profile_paths[initial_ctx.mode] = config_path

    def current(self) -> BotContext:
        with self._lock:
            return self._ctx

    def _provider_for_mode(self, mode: str) -> str:
        path = self.profile_paths.get(mode)
        if path is None or not path.exists():
            return "binance"
        try:
            with path.open("r", encoding="utf-8") as source:
                profile = json.load(source)
            return str(profile.get("broker", {}).get("provider", "binance")).lower()
        except (OSError, ValueError, TypeError):
            return "binance"

    def _demo_forward_validation(self) -> dict[str, Any]:
        path = self.profile_paths.get("demo")
        if path is None or not path.exists():
            return {
                "status": "missing",
                "ready_for_live": False,
                "summary": "Не найден Demo-профиль для forward-валидации.",
                "checks": [],
            }
        try:
            config = load_config(path)
            data_dir = Path(str(config["app"].get("data_dir", "crypto_autobot/data")))
            provider = str(config.get("broker", {}).get("provider", "binance")).lower()
            prefix = "" if provider == "binance" else f"{provider}_"
            state_path = data_dir / f"state_{prefix}demo.json"
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
            else:
                state = {
                    "created_at": None,
                    "initial_balance": float(config.get("account", {}).get("initial_balance", 0.0)),
                    "realized_pnl": 0.0,
                    "trades": [],
                }
            return forward_validation_report(config, state)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return {
                "status": "error",
                "ready_for_live": False,
                "summary": f"Не удалось проверить Demo-статистику: {exc}",
                "checks": [],
            }

    def _availability(self, mode: str) -> tuple[bool, str]:
        path = self.profile_paths.get(mode)
        if path is None or not path.exists():
            return False, f"Не найден конфиг для режима {mode.upper()}."
        provider = self._provider_for_mode(mode)
        if mode == "demo":
            if provider == "binance":
                if not os.environ.get("BINANCE_DEMO_API_KEY") or not os.environ.get("BINANCE_DEMO_API_SECRET"):
                    return False, "Сначала добавь BINANCE_DEMO_API_KEY и BINANCE_DEMO_API_SECRET."
            elif not all(os.environ.get(name) for name in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")):
                return False, "Сначала добавь MT5_LOGIN, MT5_PASSWORD и MT5_SERVER."
        if mode == "live":
            if not self.allow_live_ui:
                return False, "Live заблокирован. Запусти бота с --allow-live-ui."
            validation = self._demo_forward_validation()
            if not validation.get("ready_for_live", False):
                return False, f"Live gate: {validation.get('summary', 'Demo-валидация не пройдена.')}"
            if provider == "binance":
                if not os.environ.get("BINANCE_LIVE_API_KEY") or not os.environ.get("BINANCE_LIVE_API_SECRET"):
                    return False, "Сначала добавь BINANCE_LIVE_API_KEY и BINANCE_LIVE_API_SECRET."
            elif not all(os.environ.get(name) for name in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")):
                return False, "Сначала добавь MT5_LOGIN, MT5_PASSWORD и MT5_SERVER."
        return True, ""

    def mode_control(self, *, is_local: bool) -> dict[str, Any]:
        requires_token = bool(os.environ.get("DASHBOARD_CONTROL_TOKEN"))
        control_available = is_local or requires_token
        options: dict[str, Any] = {}
        for mode in ("paper", "demo", "live"):
            available, reason = self._availability(mode)
            provider_name = "MT5" if self._provider_for_mode(mode) == "mt5" else "Binance"
            if mode == "paper":
                summary = "Виртуальный баланс, реальные ордера не отправляются."
            elif mode == "demo":
                order_text = "разрешены" if self.orders_enabled else "заблокированы при запуске"
                summary = f"{provider_name} Demo, тестовые ордера {order_text}."
            else:
                order_text = "разрешены" if self.orders_enabled else "заблокированы при запуске"
                summary = f"Реальный {provider_name}, ордера {order_text}."
            options[mode] = {
                "available": available,
                "reason": reason,
                "summary": summary,
            }
        return {
            "options": options,
            "forward_validation": self._demo_forward_validation(),
            "requires_token": requires_token,
            "control_available": control_available,
            "control_reason": (
                ""
                if control_available
                else "На сервере задай DASHBOARD_CONTROL_TOKEN, чтобы управлять режимом через интерфейс."
            ),
            "live_confirmation": LIVE_CONFIRMATION,
        }

    def require_live_forward_gate(self) -> dict[str, Any]:
        report = self._demo_forward_validation()
        if self._ctx.mode == "live" and self.orders_enabled and not report.get("ready_for_live", False):
            raise ValueError(f"Live gate: {report.get('summary', 'Demo-валидация не пройдена.')}")
        return report

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
                pending_entries = current_state.get("pending_entries", {})
                if pending_entries:
                    symbols = ", ".join(sorted(pending_entries))
                    raise ValueError(
                        "Нельзя сменить режим, пока лимитная заявка ожидает исполнения: "
                        f"{symbols}. Дождись заполнения или отмены."
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
    host = os.environ.get("HOST", str(ctx.config["app"].get("host", "0.0.0.0")))
    port = int(os.environ.get("PORT", ctx.config["app"].get("port", 8090)))
    thread = threading.Thread(target=worker_loop, args=(controller,), daemon=True)
    thread.start()
    watchdog = threading.Thread(target=protection_watchdog_loop, args=(controller,), daemon=True)
    watchdog.start()
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
    parser = argparse.ArgumentParser(description="Crypto Autobot: Binance Futures and MT5 trading.")
    parser.add_argument("--config", default="crypto_autobot/config.example.json")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check broker, market data and MT5 order parameters without placing orders",
    )
    parser.add_argument("--enable-orders", action="store_true", help="allow broker order placement")
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
        readiness = broker_readiness_snapshot(ctx)
        print(json.dumps(readiness, indent=2))
        return 0 if readiness["ready"] else 1
    controller = RuntimeController(
        ctx,
        Path(args.config),
        orders_enabled=args.enable_orders,
        allow_live_ui=args.allow_live_ui,
    )
    try:
        controller.require_live_forward_gate()
    except ValueError as exc:
        print(f"Startup blocked: {exc}", file=sys.stderr)
        return 2
    if args.once:
        payload = scan_once(ctx)
        for result in payload["results"]:
            print(f"{result.get('symbol')}: {result.get('status')}")
        return 0
    run_server(controller)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
