#!/usr/bin/env python3
"""Run one guarded Binance Demo scan and build a privacy-safe Pages snapshot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

try:
    from .bot import build_context, scan_once
except ImportError:
    from bot import build_context, scan_once


PRIVATE_KEYS = {
    "available_balance",
    "balance",
    "equity_now",
    "initial_balance",
    "open_unrealized_pnl",
    "pnl",
    "qty",
    "realized_pnl",
    "unrealized_pnl",
}

SECRET_ENV_NAMES = (
    "BINANCE_DEMO_API_KEY",
    "BINANCE_DEMO_API_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


def _number(value: Any, digits: int = 2) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _trade_result(trade: dict[str, Any]) -> str:
    event = str(trade.get("event", "")).lower()
    if event == "open":
        return "open"
    pnl = _number(trade.get("pnl")) or 0.0
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "flat"


def _public_reason(value: Any) -> str:
    reason = str(value or "")
    return reason.split("; realized=", 1)[0]


def _safe_diagnostic(value: Any) -> str:
    message = str(value or "").replace("\n", " ").strip()
    for name in SECRET_ENV_NAMES:
        secret = os.environ.get(name, "")
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:500]


def _public_position(symbol: str, position: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(position.get("symbol") or symbol),
        "side": str(position.get("side", "unknown")),
        "entry": _number(position.get("entry"), 8),
        "stop": _number(position.get("stop"), 8),
        "target": _number(position.get("target"), 8),
        "opened_at": position.get("opened_at"),
        "external": bool(position.get("external", False)),
    }


def sanitize_public_state(
    state: dict[str, Any],
    *,
    repository: str = "",
    run_url: str = "",
) -> dict[str, Any]:
    """Keep market/strategy telemetry while excluding account-sized values."""

    broker = state.get("broker_status", {})
    stats = state.get("stats", {})
    positions = state.get("positions", {})
    latest = state.get("latest", {})
    trades = state.get("trades", [])
    logs = state.get("logs", [])

    public_positions = [
        _public_position(str(symbol), dict(position))
        for symbol, position in sorted(positions.items())
    ]
    public_latest = []
    for symbol, item in sorted(latest.items()):
        public_latest.append(
            {
                "symbol": str(item.get("symbol") or symbol),
                "price": _number(item.get("price"), 8),
                "time": item.get("time"),
                "status": str(item.get("status", "no data")),
            }
        )

    public_trades = []
    for trade in list(trades)[:30]:
        public_trades.append(
            {
                "time": trade.get("time"),
                "event": str(trade.get("event", "")),
                "symbol": str(trade.get("symbol", "")),
                "side": str(trade.get("side", "")),
                "price": _number(trade.get("price"), 8),
                "result": _trade_result(dict(trade)),
                "reason": _public_reason(trade.get("reason")),
            }
        )

    diagnostics = []
    for item in logs:
        message = _safe_diagnostic(item.get("message"))
        lowered = message.lower()
        if message and ("error" in lowered or "failed" in lowered or "disconnected" in lowered):
            diagnostics.append({"time": item.get("time"), "message": message})

    return {
        "schema_version": 1,
        "generated_at": state.get("updated_at"),
        "mode": "demo",
        "orders_enabled": bool(state.get("orders_enabled", False)),
        "broker": {
            "connected": bool(broker.get("connected", False)),
            "environment": "demo",
            "position_mode": broker.get("position_mode"),
            "message": "Connected" if broker.get("connected") else _safe_diagnostic(broker.get("message")) or "Connection problem",
        },
        "stats": {
            "return_percent": _number(stats.get("return_percent")) or 0.0,
            "closed_trades": int(stats.get("closed_trades", 0) or 0),
            "wins": int(stats.get("wins", 0) or 0),
            "losses": int(stats.get("losses", 0) or 0),
            "win_rate": _number(stats.get("win_rate")) or 0.0,
            "open_positions": int(stats.get("open_positions", len(public_positions)) or 0),
        },
        "positions": public_positions,
        "latest": public_latest,
        "trades": public_trades,
        "diagnostics": diagnostics[:10],
        "workflow": {"repository": repository, "run_url": run_url},
    }


def contains_private_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return any(key.lower() in PRIVATE_KEYS or contains_private_keys(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_private_keys(item) for item in value)
    return False


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def validate_one_shot_order_safety(config: dict[str, Any], orders_enabled: bool) -> None:
    if orders_enabled and str(config["strategy"].get("entry_order_type")) == "limit_retrace":
        raise ValueError(
            "One-shot GitHub runs cannot safely leave a limit entry waiting without continuous protection."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Binance Demo scan for GitHub Actions.")
    parser.add_argument("--config", type=Path, default=Path("crypto_autobot/config.demo.example.json"))
    parser.add_argument("--output", type=Path, default=Path("crypto_autobot/github_pages/state.json"))
    parser.add_argument("--enable-orders", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    context = build_context(args.config, orders_enabled=args.enable_orders)
    if context.mode != "demo" or context.broker is None or context.broker.environment != "demo":
        raise ValueError("GitHub runner accepts Binance Demo configuration only.")
    validate_one_shot_order_safety(context.config, args.enable_orders)

    result = scan_once(context)
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}" if repository and run_id else ""
    payload = sanitize_public_state(result["state"], repository=repository, run_url=run_url)
    if contains_private_keys(payload):
        raise RuntimeError("Privacy guard rejected the public dashboard payload.")
    write_json_atomic(args.output, payload)

    connected = payload["broker"]["connected"]
    orders = "enabled" if payload["orders_enabled"] else "disabled"
    print(f"Demo scan complete: connected={connected}, orders={orders}, symbols={len(payload['latest'])}")
    if not connected:
        print(f"Binance Demo diagnostic: {payload['broker']['message']}")
    for item in result.get("results", []):
        status = _safe_diagnostic(item.get("status"))
        if "error" in status.lower() or "failed" in status.lower():
            print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
