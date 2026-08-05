"""Forward-validation metrics and the gate that protects Live trading."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any


DEFAULT_RULES: dict[str, float | int] = {
    "min_observation_days": 30,
    "min_closed_trades": 100,
    "min_trades_per_day": 3.5,
    "max_trades_per_day": 6.5,
    "min_win_rate_percent": 45.0,
    "min_profit_factor": 1.10,
    "max_drawdown_percent": 10.0,
    "min_return_percent": 0.0,
    "min_nominal_reward_risk": 1.50,
    "min_daily_data_coverage_percent": 75.0,
}


def _timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _max_drawdown_percent(initial_balance: float, closed_trades: list[dict[str, Any]]) -> float:
    if initial_balance <= 0:
        return 0.0
    equity = initial_balance
    peak = equity
    max_drawdown = 0.0
    for trade in closed_trades:
        equity += float(trade.get("pnl", 0.0))
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
    return max_drawdown


def _is_validation_trade(trade: dict[str, Any]) -> bool:
    return str(trade.get("source", "baseline")) != "manual_demo_test"


def _interval_milliseconds(interval: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    unit = interval[-1]
    if unit not in units:
        raise ValueError(f"Unsupported interval: {interval}")
    return int(interval[:-1]) * units[unit]


def forward_validation_report(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    rules = {**DEFAULT_RULES, **dict(config.get("forward_validation", {}))}
    timezone = dt.timezone.utc
    started_at = _timestamp(state.get("validation_started_at") or state.get("created_at"))
    if started_at is not None:
        timezone = started_at.tzinfo or dt.timezone.utc
    current = now or dt.datetime.now(timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    calendar_days = (
        max(0.0, (current - started_at).total_seconds() / 86_400.0)
        if started_at is not None
        else 0.0
    )
    coverage_rows = state.get("validation_coverage", {})
    coverage_required = None
    qualified_dates: list[str] = []
    current_date = current.astimezone(timezone).date().isoformat()
    current_date_coverage = 0
    coverage_enabled = "validation_coverage" in state and isinstance(coverage_rows, dict)
    if coverage_enabled:
        interval = str(config.get("market", {}).get("interval", "15m"))
        symbols = list(config.get("market", {}).get("symbols", []))
        expected_per_day = max(
            1,
            int(86_400_000 / _interval_milliseconds(interval)) * max(1, len(symbols)),
        )
        coverage_required = math.ceil(
            expected_per_day
            * float(rules["min_daily_data_coverage_percent"])
            / 100.0
        )
        current_row = coverage_rows.get(current_date, {})
        if isinstance(current_row, dict):
            current_date_coverage = max(0, int(current_row.get("symbol_candles", 0)))
        qualified_dates = sorted(
            str(date_key)
            for date_key, row in coverage_rows.items()
            if str(date_key) < current_date
            and isinstance(row, dict)
            and int(row.get("symbol_candles", 0)) >= coverage_required
        )
        observation_days = float(len(qualified_dates))
    else:
        active_dates = {
            str(value)
            for value in state.get("validation_active_dates", [])
            if str(value).strip()
        }
        observation_days = float(len(active_dates)) if active_dates else calendar_days

    trades = list(state.get("trades", []))
    daily_map = {
        str(date_key): row
        for date_key, row in state.get("daily", {}).items()
        if isinstance(row, dict)
    }
    if coverage_enabled:
        qualified_rows = [daily_map[date_key] for date_key in qualified_dates if date_key in daily_map]
        opened_count = sum(
            max(0, int(row.get("validation_trades", row.get("trades", 0))))
            for row in qualified_rows
        )
        pnl_values = [
            float(pnl)
            for row in qualified_rows
            for pnl in row.get("validation_pnls", [])
        ]
        if not any("validation_pnls" in row for row in qualified_rows):
            qualified_set = set(qualified_dates)
            qualified_trades = []
            for item in trades:
                timestamp = _timestamp(item.get("time"))
                if timestamp is None or timestamp.astimezone(timezone).date().isoformat() not in qualified_set:
                    continue
                qualified_trades.append(item)
            if not qualified_rows:
                opened_count = sum(
                    item.get("event") == "open" and _is_validation_trade(item)
                    for item in qualified_trades
                )
            pnl_values = [
                float(item.get("pnl", 0.0))
                for item in qualified_trades
                if item.get("event") == "close" and _is_validation_trade(item)
            ]
    else:
        opened = [
            item
            for item in trades
            if item.get("event") == "open" and _is_validation_trade(item)
        ]
        closed = [
            item
            for item in trades
            if item.get("event") == "close" and _is_validation_trade(item)
        ]
        daily_rows = list(daily_map.values())
        has_daily_open_totals = any("validation_trades" in item for item in daily_rows)
        daily_opened = sum(
            max(
                0,
                int(
                    item.get("validation_trades", 0)
                    if has_daily_open_totals
                    else item.get("trades", 0)
                ),
            )
            for item in daily_rows
        )
        opened_count = max(len(opened), daily_opened)
        pnl_values = [float(item.get("pnl", 0.0)) for item in closed]

    closed_count = len(pnl_values)
    wins_count = sum(pnl > 0 for pnl in pnl_values)
    gross_profit = sum(pnl for pnl in pnl_values if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnl_values if pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    profit_factor_infinite = gross_profit > 0 and gross_loss == 0
    win_rate = wins_count / closed_count * 100.0 if closed_count else 0.0
    trades_per_day = opened_count / max(observation_days, 1.0)
    initial_balance = float(state.get("initial_balance", 0.0))
    realized_pnl = sum(pnl_values)
    return_percent = realized_pnl / initial_balance * 100.0 if initial_balance > 0 else 0.0
    drawdown = _max_drawdown_percent(
        initial_balance,
        [{"pnl": pnl} for pnl in pnl_values],
    )
    strategy = config.get("strategy", {})
    stop_atr = float(strategy.get("stop_atr", 0.0))
    target_atr = float(strategy.get("target_atr", 0.0))
    nominal_rr = target_atr / stop_atr if stop_atr > 0 else 0.0

    enough_days = observation_days >= float(rules["min_observation_days"])
    enough_trades = closed_count >= int(rules["min_closed_trades"])
    checks = [
        {
            "id": "observation_days",
            "label": "Период Demo",
            "value": round(observation_days, 2),
            "target": f">= {int(rules['min_observation_days'])} дней",
            "passed": enough_days,
        },
        {
            "id": "closed_trades",
            "label": "Закрытые сделки",
            "value": closed_count,
            "target": f">= {int(rules['min_closed_trades'])}",
            "passed": enough_trades,
        },
        {
            "id": "trades_per_day",
            "label": "Сделок в день",
            "value": round(trades_per_day, 2),
            "target": f"{float(rules['min_trades_per_day']):g}-{float(rules['max_trades_per_day']):g}",
            "passed": (
                enough_days
                and float(rules["min_trades_per_day"])
                <= trades_per_day
                <= float(rules["max_trades_per_day"])
            ),
        },
        {
            "id": "win_rate",
            "label": "Win rate",
            "value": round(win_rate, 2),
            "target": f">= {float(rules['min_win_rate_percent']):g}%",
            "passed": enough_trades and win_rate >= float(rules["min_win_rate_percent"]),
        },
        {
            "id": "profit_factor",
            "label": "Profit factor",
            "value": None if profit_factor is None else round(profit_factor, 3),
            "display_value": "infinity" if profit_factor_infinite else None,
            "target": f">= {float(rules['min_profit_factor']):g}",
            "passed": enough_trades and (
                profit_factor_infinite
                or (profit_factor is not None and profit_factor >= float(rules["min_profit_factor"]))
            ),
        },
        {
            "id": "return_percent",
            "label": "Доходность",
            "value": round(return_percent, 2),
            "target": f"> {float(rules['min_return_percent']):g}%",
            "passed": enough_trades and return_percent > float(rules["min_return_percent"]),
        },
        {
            "id": "max_drawdown",
            "label": "Макс. просадка",
            "value": round(drawdown, 2),
            "target": f"<= {float(rules['max_drawdown_percent']):g}%",
            "passed": enough_trades and drawdown <= float(rules["max_drawdown_percent"]),
        },
        {
            "id": "nominal_rr",
            "label": "Номинальный RR",
            "value": round(nominal_rr, 2),
            "target": f">= 1:{float(rules['min_nominal_reward_risk']):g}",
            "passed": nominal_rr >= float(rules["min_nominal_reward_risk"]),
        },
    ]
    ready = all(bool(item["passed"]) for item in checks)
    if ready:
        status = "passed"
        summary = "Demo-критерии выполнены. Live всё ещё требует ручной разблокировки."
    elif not enough_days or not enough_trades:
        status = "collecting"
        summary = (
            f"Собираем Demo-выборку: {closed_count}/{int(rules['min_closed_trades'])} сделок, "
            f"{observation_days:.1f}/{int(rules['min_observation_days'])} дней."
        )
        if coverage_required is not None:
            summary += (
                f" Покрытие сегодня: {current_date_coverage}/{coverage_required} "
                "закрытых свечей."
            )
    else:
        status = "failed"
        summary = "Выборка достаточна, но стратегия не прошла все критерии Live."

    return {
        "status": status,
        "ready_for_live": ready,
        "summary": summary,
        "started_at": started_at.isoformat() if started_at else None,
        "observation_days": round(observation_days, 2),
        "qualified_observation_dates": qualified_dates,
        "daily_coverage_required": coverage_required,
        "current_date": current_date,
        "current_date_coverage": current_date_coverage,
        "daily_coverage": coverage_rows if isinstance(coverage_rows, dict) else {},
        "opened_trades": opened_count,
        "closed_trades": closed_count,
        "trades_per_day": round(trades_per_day, 2),
        "win_rate": round(win_rate, 2),
        "profit_factor": None if profit_factor is None else round(profit_factor, 3),
        "profit_factor_infinite": profit_factor_infinite,
        "return_percent": round(return_percent, 2),
        "max_drawdown_percent": round(drawdown, 2),
        "nominal_reward_risk": round(nominal_rr, 2),
        "checks": checks,
    }
