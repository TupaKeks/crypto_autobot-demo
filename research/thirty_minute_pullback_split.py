#!/usr/bin/env python3
"""Three-way audit of the short pullback model on 30-minute bars."""

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

from bot import Candle  # noqa: E402
from portfolio_backtest import prepare_histories, run_portfolio_backtest  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics, load_cached_histories  # noqa: E402

PROFILES = {
    "time_equivalent": (5, 11, 24, 72, 6, 6),
    "slow": (9, 21, 48, 144, 12, 12),
}


def aggregate_30m(candles: list[Candle]) -> list[Candle]:
    buckets: dict[int, list[Candle]] = {}
    period = 30 * 60_000
    for candle in candles:
        bucket = candle.open_time - candle.open_time % period
        buckets.setdefault(bucket, []).append(candle)
    result: list[Candle] = []
    for bucket in sorted(buckets):
        rows = sorted(buckets[bucket], key=lambda item: item.open_time)
        if len(rows) != 2 or rows[1].open_time - rows[0].open_time != 15 * 60_000:
            continue
        result.append(
            Candle(
                open_time=bucket,
                open=rows[0].open,
                high=max(item.high for item in rows),
                low=min(item.low for item in rows),
                close=rows[-1].close,
                volume=sum(item.volume for item in rows),
                close_time=rows[-1].close_time,
                quote_volume=sum(item.quote_volume for item in rows),
                trade_count=sum(item.trade_count for item in rows),
                taker_buy_volume=sum(item.taker_buy_volume for item in rows),
                taker_buy_quote_volume=sum(item.taker_buy_quote_volume for item in rows),
            )
        )
    return result


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


def make_strategy(base: dict[str, Any], values: tuple[Any, ...]) -> dict[str, Any]:
    profile, min_adx, volume_factor, body_ratio, trigger_rsi = values
    entry_fast, entry_slow, regime_fast, regime_slow, slope_bars, lookback = PROFILES[profile]
    strategy = dict(base)
    strategy.update(
        {
            "type": "intraday_regime_pullback",
            "entry_order_type": "limit_retrace",
            "entry_offset_atr": 0.1,
            "entry_expiry_bars": 1,
            "target_order_type": "limit",
            "entry_fast_ema": entry_fast,
            "entry_slow_ema": entry_slow,
            "regime_fast_ema": regime_fast,
            "regime_slow_ema": regime_slow,
            "regime_slope_bars": slope_bars,
            "pullback_lookback": lookback,
            "min_adx": min_adx,
            "min_volume_factor": volume_factor,
            "min_confirmation_body_ratio": body_ratio,
            "short_trigger_rsi": trigger_rsi,
            "stop_atr": 1.8,
            "target_atr": 2.8,
            "max_holding_bars": 12,
            "cooldown_bars": 2,
            "allow_longs": False,
            "allow_shorts": True,
        }
    )
    return strategy


def main() -> int:
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text(encoding="utf-8"))
    cache_dir = ROOT / "data/market_cache_15m_430d"
    symbols = sorted(path.name.split("-")[0] for path in cache_dir.glob("*-15m-*.json"))
    source = load_cached_histories(cache_dir, symbols, "15m")
    histories = {symbol: aggregate_30m(candles) for symbol, candles in source.items()}
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    development_start = data_end - 420 * DAY_MS
    validation_start = data_end - 180 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS
    account = dict(config["account"])
    broker = dict(config["broker"])

    grid = list(
        itertools.product(
            tuple(PROFILES),
            (12.0, 18.0),
            (0.7, 1.0),
            (0.1, 0.25),
            (55.0, 58.0),
        )
    )
    prepared_by_profile = {
        profile: prepare_histories(
            histories,
            make_strategy(config["strategy"], next(values for values in grid if values[0] == profile)),
        )
        for profile in PROFILES
    }
    candidates: list[dict[str, Any]] = []
    for values in grid:
        strategy = make_strategy(config["strategy"], values)
        result = run_portfolio_backtest(
            histories,
            strategy,
            account,
            broker,
            development_start,
            validation_start,
            fee_bps=5.0,
            maker_fee_bps=2.0,
            slippage_bps=2.0,
            prepared_data=prepared_by_profile[str(values[0])],
        )
        months = monthly(result["trade_log"], development_start, 8)
        count = int(result["trades"])
        score = float(result["average_realized_r"]) - 1.0 / math.sqrt(max(count, 1))
        candidates.append(
            {
                "parameters": {
                    "profile": values[0],
                    "min_adx": values[1],
                    "min_volume_factor": values[2],
                    "min_body_ratio": values[3],
                    "short_trigger_rsi": values[4],
                },
                "development": compact(result),
                "profitable_months": sum(float(item["net_pnl"]) > 0 for item in months),
                "conservative_score": round(score, 6),
            }
        )

    eligible = [
        item
        for item in candidates
        if 3.5 <= float(item["development"]["trades_per_day"] or 0) <= 6.5
        and float(item["development"]["profit_factor"] or 0) >= 1.1
        and int(item["profitable_months"]) >= 5
    ]
    selected = max(eligible or candidates, key=lambda item: float(item["conservative_score"]))
    p = selected["parameters"]
    strategy = make_strategy(
        config["strategy"],
        (p["profile"], p["min_adx"], p["min_volume_factor"], p["min_body_ratio"], p["short_trigger_rsi"]),
    )
    prepared = prepared_by_profile[str(p["profile"])]
    common = {
        "histories": histories,
        "strategy": strategy,
        "account": account,
        "broker": broker,
        "prepared_data": prepared,
    }
    validation = run_portfolio_backtest(
        start_ms=validation_start,
        end_ms=holdout_start,
        fee_bps=5.0,
        maker_fee_bps=2.0,
        slippage_bps=2.0,
        **common,
    )
    validation_stress = run_portfolio_backtest(
        start_ms=validation_start,
        end_ms=holdout_start,
        fee_bps=5.0,
        maker_fee_bps=4.0,
        slippage_bps=3.0,
        **common,
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
            "description": "240d development, 120d validation, sealed 60d holdout on 30m candles",
            "candidate_count": len(candidates),
            "symbols": symbols,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "selected_on_development": selected,
        "strategy": strategy,
        "validation_120d": compact(validation),
        "validation_stress_maker4_slippage3": compact(validation_stress),
        "validation_months": validation_months,
        "validation_passed": validation_passed,
        "holdout_opened": validation_passed,
        "development_candidates": sorted(
            candidates, key=lambda item: float(item["conservative_score"]), reverse=True
        ),
    }
    if validation_passed:
        holdout = run_portfolio_backtest(
            start_ms=holdout_start,
            end_ms=data_end,
            fee_bps=5.0,
            maker_fee_bps=2.0,
            slippage_bps=2.0,
            **common,
        )
        holdout_stress = run_portfolio_backtest(
            start_ms=holdout_start,
            end_ms=data_end,
            fee_bps=5.0,
            maker_fee_bps=4.0,
            slippage_bps=3.0,
            **common,
        )
        report["final_holdout_once"] = compact(holdout)
        report["final_holdout_stress"] = compact(holdout_stress)
        report["holdout_months"] = monthly(holdout["trade_log"], holdout_start, 2)

    output = ROOT / "research/thirty_minute_pullback_split.json"
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
