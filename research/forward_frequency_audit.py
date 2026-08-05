#!/usr/bin/env python3
"""Compare recent Binance production and Demo strategy frequency."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import load_config  # noqa: E402
from portfolio_backtest import (  # noqa: E402
    fetch_history,
    interval_ms,
    prepare_histories,
    run_portfolio_backtest,
)
from strategy_intraday import minimum_history  # noqa: E402


ENVIRONMENTS = {
    "production": "https://fapi.binance.com",
    "demo": "https://demo-fapi.binance.com",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--config",
        default=str(ROOT / "config.paper.asymmetric-15m.example.json"),
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "research/forward_frequency_latest.json"),
    )
    return parser.parse_args()


def cache_history(
    cache_dir: Path,
    base_url: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{symbol}-{interval}-{start_ms}-{end_ms}.json"
    if path.exists():
        from bot import Candle

        return [Candle(**row) for row in json.loads(path.read_text(encoding="utf-8"))]
    candles = fetch_history(base_url, symbol, interval, start_ms, end_ms)
    path.write_text(
        json.dumps([dataclasses.asdict(candle) for candle in candles]),
        encoding="utf-8",
    )
    return candles


def compact(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "trades",
            "trades_per_day",
            "win_rate",
            "profit_factor",
            "return_percent",
            "max_drawdown_percent",
        )
    }


def close_divergence_bps(
    production: dict[str, list[Any]],
    demo: dict[str, list[Any]],
) -> dict[str, Any]:
    by_symbol: dict[str, float] = {}
    observations = 0
    total = 0.0
    for symbol, prod_candles in production.items():
        demo_by_time = {item.open_time: item for item in demo.get(symbol, [])}
        values = []
        for candle in prod_candles:
            other = demo_by_time.get(candle.open_time)
            if other is None or candle.close == 0:
                continue
            values.append(abs(other.close / candle.close - 1.0) * 10_000.0)
        if values:
            by_symbol[symbol] = round(sum(values) / len(values), 3)
            observations += len(values)
            total += sum(values)
    return {
        "mean_absolute_close_divergence_bps": round(total / observations, 3) if observations else None,
        "observations": observations,
        "by_symbol_bps": by_symbol,
    }


def main() -> int:
    args = parse_args()
    if args.days < 2:
        raise ValueError("Use at least two audit days.")
    config = load_config(Path(args.config))
    interval = str(config["market"]["interval"])
    now_ms = int(time.time() * 1000)
    closed_end = now_ms // interval_ms(interval) * interval_ms(interval)
    warmup_bars = minimum_history(config["strategy"]) + 20
    start_ms = closed_end - (args.days * 86_400_000 + warmup_bars * interval_ms(interval))
    histories: dict[str, dict[str, list[Any]]] = {}
    for environment, base_url in ENVIRONMENTS.items():
        histories[environment] = {
            symbol: cache_history(
                ROOT / "data/forward_frequency_cache" / environment,
                base_url,
                symbol,
                interval,
                start_ms,
                closed_end,
            )
            for symbol in config["market"]["symbols"]
        }

    periods: dict[str, dict[str, Any]] = {}
    for environment, environment_histories in histories.items():
        prepared = prepare_histories(environment_histories, config["strategy"])
        period_rows: dict[str, Any] = {}
        for days in sorted({2, 3, args.days}):
            if days > args.days:
                continue
            result = run_portfolio_backtest(
                environment_histories,
                config["strategy"],
                config["account"],
                config["broker"],
                closed_end - days * 86_400_000,
                closed_end,
                5.0,
                2.0,
                maker_fee_bps=2.0,
                prepared_data=prepared,
            )
            period_rows[f"{days}d"] = compact(result)
        periods[environment] = period_rows

    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "period_end": dt.datetime.fromtimestamp(closed_end / 1000, dt.timezone.utc).isoformat(),
        "symbols": config["market"]["symbols"],
        "strategy": "asymmetric 15m regime pullback",
        "periods": periods,
        "environment_divergence": close_divergence_bps(
            histories["production"], histories["demo"]
        ),
        "warning": "Short recent samples are diagnostic and do not establish profitability.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
