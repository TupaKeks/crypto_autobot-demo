#!/usr/bin/env python3
"""Fee-aware three-way audit of the short pullback model on five-minute bars."""

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

from portfolio_backtest import load_history, prepare_histories, run_portfolio_backtest  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics  # noqa: E402


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


REGIME_PROFILES = {
    "fast": (9, 21, 48, 144, 12, (12, 18)),
    "medium": (18, 42, 96, 288, 24, (18, 24)),
    "scaled": (27, 63, 144, 432, 36, (24, 36)),
}


def make_strategy(base: dict[str, Any], values: tuple[Any, ...]) -> dict[str, Any]:
    profile, lookback, min_adx, volume_factor, body_ratio, trigger_rsi = values
    entry_fast, entry_slow, regime_fast, regime_slow, slope_bars, _ = REGIME_PROFILES[profile]
    strategy = dict(base)
    strategy.update(
        {
            "type": "intraday_regime_pullback",
            "entry_order_type": "limit_retrace",
            "entry_offset_atr": 0.1,
            "entry_expiry_bars": 3,
            "target_order_type": "limit",
            "entry_fast_ema": entry_fast,
            "entry_slow_ema": entry_slow,
            "regime_fast_ema": regime_fast,
            "regime_slow_ema": regime_slow,
            "regime_slope_bars": slope_bars,
            "min_regime_slope_percent": 0.02,
            "pullback_lookback": lookback,
            "short_pullback_rsi": 60.0,
            "short_trigger_rsi": trigger_rsi,
            "max_entry_extension_atr": 1.0,
            "min_adx": min_adx,
            "min_volume_factor": volume_factor,
            "min_confirmation_body_ratio": body_ratio,
            "stop_atr": 2.4,
            "target_atr": 3.8,
            "max_holding_bars": 72,
            "cooldown_bars": 9,
            "allow_longs": False,
            "allow_shorts": True,
        }
    )
    return strategy


def main() -> int:
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text(encoding="utf-8"))
    symbols = list(config["market"]["symbols"])
    reference_cache = sorted((ROOT / "data/market_cache_15m_430d").glob("*-15m-*.json"))
    if not reference_cache:
        raise RuntimeError("15m reference cache is missing")
    data_end = min(int(path.stem.rsplit("-", 1)[1]) for path in reference_cache)
    development_start = data_end - 240 * DAY_MS
    validation_start = data_end - 120 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS
    fetch_start = development_start - 5 * DAY_MS
    cache_dir = ROOT / "data/market_cache_5m_245d"
    histories = {}
    for symbol in symbols:
        histories[symbol] = load_history(cache_dir, symbol, "5m", fetch_start, data_end)
        print(f"loaded {symbol}: {len(histories[symbol])} candles", flush=True)

    account = dict(config["account"])
    broker = dict(config["broker"])
    grid = [
        (profile, lookback, min_adx, volume_factor, body_ratio, trigger_rsi)
        for profile, values in REGIME_PROFILES.items()
        for lookback, min_adx, volume_factor, body_ratio, trigger_rsi in itertools.product(
            values[5], (12.0, 18.0), (0.7, 1.0), (0.1, 0.25), (55.0, 58.0)
        )
    ]
    prepared_by_profile = {
        profile: prepare_histories(
            histories,
            make_strategy(config["strategy"], next(values for values in grid if values[0] == profile)),
        )
        for profile in REGIME_PROFILES
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
        months = monthly(result["trade_log"], development_start, 4)
        count = int(result["trades"])
        score = float(result["average_realized_r"]) - 1.0 / math.sqrt(max(count, 1))
        candidates.append(
            {
                "parameters": {
                    "regime_profile": values[0],
                    "pullback_lookback": values[1],
                    "min_adx": values[2],
                    "min_volume_factor": values[3],
                    "min_body_ratio": values[4],
                    "short_trigger_rsi": values[5],
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
        and int(item["profitable_months"]) >= 3
    ]
    selected = max(eligible or candidates, key=lambda item: float(item["conservative_score"]))
    p = selected["parameters"]
    strategy = make_strategy(
        config["strategy"],
        (
            p["regime_profile"],
            p["pullback_lookback"],
            p["min_adx"],
            p["min_volume_factor"],
            p["min_body_ratio"],
            p["short_trigger_rsi"],
        ),
    )
    prepared = prepared_by_profile[str(p["regime_profile"])]
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
    validation_months = monthly(validation["trade_log"], validation_start, 2)
    validation_passed = (
        3.5 <= float(validation["trades_per_day"]) <= 6.5
        and float(validation["profit_factor"] or 0) >= 1.1
        and float(validation_stress["profit_factor"] or 0) >= 1.0
        and sum(float(item["net_pnl"]) > 0 for item in validation_months) == 2
    )
    report: dict[str, Any] = {
        "method": {
            "description": "120d development, 60d validation, sealed 60d holdout on 5m candles",
            "candidate_count": len(candidates),
            "symbols": symbols,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "selected_on_development": selected,
        "strategy": strategy,
        "validation_60d": compact(validation),
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

    output = ROOT / "research/five_minute_pullback_split.json"
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
