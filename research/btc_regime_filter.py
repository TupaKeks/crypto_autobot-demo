#!/usr/bin/env python3
"""No-lookahead audit of BTC regime gates for long pullback signals."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import Candle  # noqa: E402
from portfolio_backtest import prepare_histories, run_portfolio_backtest  # noqa: E402
from rolling_universe import DAY_MS, basic_metrics, load_cached_histories  # noqa: E402
from strategy_intraday import build_indicators  # noqa: E402


def compact(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "trades",
            "trades_per_day",
            "win_rate",
            "profit_factor",
            "average_realized_r",
            "return_percent",
            "max_drawdown_percent",
        )
    }


def main() -> int:
    config = json.loads((ROOT / "config.demo.regime-scalp.example.json").read_text(encoding="utf-8"))
    strategy = dict(config["strategy"])
    cache_dir = ROOT / "data/market_cache_15m_430d"
    symbols = list(config["market"]["symbols"])
    histories = load_cached_histories(cache_dir, symbols, "15m")
    btc = load_cached_histories(cache_dir, ["BTCUSDT"], "15m")["BTCUSDT"]
    btc_index = {candle.open_time: index for index, candle in enumerate(btc)}
    btc_indicators = build_indicators(btc, strategy)
    prepared = prepare_histories(histories, strategy)

    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    validation_start = data_end - 330 * DAY_MS
    holdout_start = data_end - 60 * DAY_MS

    def values(timestamp: int) -> tuple[int, Candle, float, float, float, float] | None:
        index = btc_index.get(timestamp)
        if index is None or index < 96:
            return None
        needed = (
            btc_indicators.entry_fast[index],
            btc_indicators.entry_slow[index],
            btc_indicators.regime_fast[index],
            btc_indicators.regime_slow[index],
        )
        if any(value is None for value in needed):
            return None
        return (
            index,
            btc[index],
            float(needed[0]),
            float(needed[1]),
            float(needed[2]),
            float(needed[3]),
        )

    def short_only(_symbol: str, side: str, _timestamp: int) -> bool:
        return side == "short"

    def above_slow(_symbol: str, side: str, timestamp: int) -> bool:
        item = values(timestamp)
        return side == "short" or bool(item and item[1].close > item[5])

    def aligned_regime(_symbol: str, side: str, timestamp: int) -> bool:
        item = values(timestamp)
        return side == "short" or bool(item and item[1].close > item[5] and item[4] > item[5])

    def aligned_entries(_symbol: str, side: str, timestamp: int) -> bool:
        item = values(timestamp)
        return side == "short" or bool(
            item and item[1].close > item[2] > item[3] and item[4] > item[5]
        )

    def positive_day(_symbol: str, side: str, timestamp: int) -> bool:
        item = values(timestamp)
        return side == "short" or bool(item and item[1].close > btc[item[0] - 96].close)

    def aligned_positive_day(_symbol: str, side: str, timestamp: int) -> bool:
        item = values(timestamp)
        return side == "short" or bool(
            item
            and item[1].close > item[5]
            and item[4] > item[5]
            and item[1].close > btc[item[0] - 96].close
        )

    filters: dict[str, Callable[[str, str, int], bool] | None] = {
        "no_filter": None,
        "short_only": short_only,
        "long_btc_above_ema144": above_slow,
        "long_btc_regime_aligned": aligned_regime,
        "long_btc_entries_and_regime_aligned": aligned_entries,
        "long_btc_positive_24h": positive_day,
        "long_btc_regime_and_positive_24h": aligned_positive_day,
    }
    common = {
        "histories": histories,
        "strategy": strategy,
        "account": config["account"],
        "broker": config["broker"],
        "prepared_data": prepared,
    }
    candidates: list[dict[str, Any]] = []
    for name, signal_filter in filters.items():
        base = run_portfolio_backtest(
            start_ms=validation_start,
            end_ms=holdout_start,
            fee_bps=5.0,
            maker_fee_bps=2.0,
            slippage_bps=2.0,
            signal_filter=signal_filter,
            **common,
        )
        stress = run_portfolio_backtest(
            start_ms=validation_start,
            end_ms=holdout_start,
            fee_bps=5.0,
            maker_fee_bps=4.0,
            slippage_bps=3.0,
            signal_filter=signal_filter,
            **common,
        )
        candidates.append(
            {
                "name": name,
                "validation": compact(base),
                "validation_stress_maker4_slippage3": compact(stress),
                "profitable_months": sum(
                    float(item["net_pnl"]) > 0
                    for item in (
                        basic_metrics(
                            base["trade_log"],
                            validation_start + month * 30 * DAY_MS,
                            validation_start + (month + 1) * 30 * DAY_MS,
                        )
                        for month in range(9)
                    )
                ),
            }
        )

    eligible = [
        item
        for item in candidates
        if 3.5 <= float(item["validation"]["trades_per_day"] or 0) <= 5.5
        and float(item["validation"]["profit_factor"] or 0) > 1.05
        and float(item["validation_stress_maker4_slippage3"]["profit_factor"] or 0) > 1.0
        and int(item["profitable_months"]) >= 5
    ]
    ranked = eligible or candidates
    selected = max(
        ranked,
        key=lambda item: (
            min(
                float(item["validation"]["profit_factor"] or 0),
                float(item["validation_stress_maker4_slippage3"]["profit_factor"] or 0),
            ),
            int(item["profitable_months"]),
        ),
    )
    selected_filter = filters[str(selected["name"])]
    holdout = run_portfolio_backtest(
        start_ms=holdout_start,
        end_ms=data_end,
        fee_bps=5.0,
        maker_fee_bps=2.0,
        slippage_bps=2.0,
        signal_filter=selected_filter,
        **common,
    )
    holdout_stress = run_portfolio_backtest(
        start_ms=holdout_start,
        end_ms=data_end,
        fee_bps=5.0,
        maker_fee_bps=4.0,
        slippage_bps=3.0,
        signal_filter=selected_filter,
        **common,
    )
    report = {
        "method": {
            "description": "BTC trend gate applies only to long signals; shorts stay unchanged",
            "selection_period": "270 days",
            "final_holdout_days": 60,
            "holdout_used_in_selection": False,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "candidates": candidates,
        "selected": selected,
        "final_holdout_once": compact(holdout),
        "final_holdout_stress_maker4_slippage3": compact(holdout_stress),
        "holdout_months": [
            basic_metrics(
                holdout["trade_log"],
                holdout_start + month * 30 * DAY_MS,
                holdout_start + (month + 1) * 30 * DAY_MS,
            )
            for month in range(2)
        ],
    }
    output = ROOT / "research/btc_regime_filter.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("selected", "final_holdout_once", "final_holdout_stress_maker4_slippage3", "holdout_months")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
