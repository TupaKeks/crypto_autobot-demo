from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from crypto_autobot.bot import Candle
from crypto_autobot.portfolio_backtest import run_portfolio_backtest
from crypto_autobot.strategy_intraday import SignalDecision, build_indicators


def candle(index: int, open_: float, high: float, low: float, close: float) -> Candle:
    open_time = index * 300_000
    return Candle(open_time, open_, high, low, close, 1_000.0, open_time + 299_999)


class LimitExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = {
            "type": "intraday_mean_reversion",
            "entry_order_type": "limit_retrace",
            "entry_offset_atr": 0.5,
            "entry_expiry_bars": 1,
            "stop_atr": 1.0,
            "target_atr": 2.0,
            "max_holding_bars": 10,
            "cooldown_bars": 0,
        }
        self.account = {
            "initial_balance": 1_000.0,
            "risk_per_trade_percent": 1.0,
            "max_open_positions": 1,
            "max_daily_trades": 5,
            "max_daily_loss_percent": 5.0,
            "same_candle_exit": "stop_first",
        }
        self.broker = {"leverage": 2}

    @staticmethod
    def decision(_candles, index, _strategy, _indicators):
        if index == 0:
            return SignalDecision("long", "signal", "test", 10.0, 10.0)
        return SignalDecision(None, "waiting")

    def run_case(
        self,
        candles: list[Candle],
        active_universe_periods: list[tuple[int, int, set[str]]] | None = None,
        signal_filter=None,
    ) -> dict:
        prepared = {
            "BTCUSDT": {
                "candles": candles,
                "indicators": object(),
                "index_by_time": {item.open_time: index for index, item in enumerate(candles)},
            }
        }
        with (
            patch("crypto_autobot.portfolio_backtest.minimum_history", return_value=0),
            patch("crypto_autobot.portfolio_backtest.evaluate_strategy_signal", side_effect=self.decision),
        ):
            return run_portfolio_backtest(
                {"BTCUSDT": candles},
                self.strategy,
                self.account,
                self.broker,
                0,
                900_000,
                0.0,
                0.0,
                (prepared, {item.open_time for item in candles}),
                maker_fee_bps=0.0,
                active_universe_periods=active_universe_periods,
                signal_filter=signal_filter,
            )

    def test_limit_entry_waits_for_a_later_candle_and_fills(self) -> None:
        result = self.run_case(
            [
                candle(0, 99.0, 101.0, 98.0, 100.0),
                candle(1, 100.0, 104.0, 94.0, 101.0),
                candle(2, 101.0, 116.0, 100.0, 115.0),
            ]
        )
        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["execution"]["limit_fills"], 1)
        self.assertEqual(result["trade_log"][0]["entry"], 95.0)
        self.assertEqual(result["trade_log"][0]["entry_time"], 300_000)
        self.assertEqual(result["trade_log"][0]["reason"], "target")

    def test_long_specific_risk_controls_backtest_position_size(self) -> None:
        self.account["long_risk_per_trade_percent"] = 0.25
        result = self.run_case(
            [
                candle(0, 99.0, 101.0, 98.0, 100.0),
                candle(1, 100.0, 104.0, 94.0, 101.0),
                candle(2, 101.0, 116.0, 100.0, 115.0),
            ]
        )

        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["trade_log"][0]["quantity"], 0.25)

    def test_unfilled_limit_expires_without_a_trade(self) -> None:
        result = self.run_case(
            [
                candle(0, 99.0, 101.0, 98.0, 100.0),
                candle(1, 100.0, 104.0, 96.0, 101.0),
                candle(2, 101.0, 116.0, 96.0, 115.0),
            ]
        )
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["execution"]["limit_fills"], 0)
        self.assertEqual(result["execution"]["limit_expired"], 1)

    def test_inactive_symbol_cannot_create_an_entry(self) -> None:
        result = self.run_case(
            [
                candle(0, 99.0, 101.0, 98.0, 100.0),
                candle(1, 100.0, 104.0, 94.0, 101.0),
                candle(2, 101.0, 116.0, 100.0, 115.0),
            ],
            active_universe_periods=[(0, 900_000, set())],
        )
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["execution"]["signals"], 0)

    def test_rejected_signal_cannot_create_an_entry(self) -> None:
        result = self.run_case(
            [
                candle(0, 99.0, 101.0, 98.0, 100.0),
                candle(1, 100.0, 104.0, 94.0, 101.0),
                candle(2, 101.0, 116.0, 100.0, 115.0),
            ],
            signal_filter=lambda _symbol, _side, _timestamp: False,
        )
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["execution"]["signals"], 0)
        self.assertEqual(result["execution"]["limit_orders"], 0)

    def test_limit_target_uses_maker_fee_without_slippage(self) -> None:
        self.strategy["target_order_type"] = "limit"
        candles = [
            candle(0, 99.0, 101.0, 98.0, 100.0),
            candle(1, 100.0, 104.0, 94.0, 101.0),
            candle(2, 101.0, 116.0, 100.0, 115.0),
        ]
        prepared = {
            "BTCUSDT": {
                "candles": candles,
                "indicators": object(),
                "index_by_time": {item.open_time: index for index, item in enumerate(candles)},
            }
        }
        with (
            patch("crypto_autobot.portfolio_backtest.minimum_history", return_value=0),
            patch("crypto_autobot.portfolio_backtest.evaluate_strategy_signal", side_effect=self.decision),
        ):
            result = run_portfolio_backtest(
                {"BTCUSDT": candles},
                self.strategy,
                self.account,
                self.broker,
                0,
                900_000,
                5.0,
                2.0,
                (prepared, {item.open_time for item in candles}),
                maker_fee_bps=2.0,
            )

        trade = result["trade_log"][0]
        self.assertEqual(trade["exit"], 115.0)
        self.assertEqual(trade["fees"], 0.042)
        self.assertEqual(result["execution"]["maker_target_fills"], 1)


class IndicatorCausalityTests(unittest.TestCase):
    def test_future_candles_do_not_change_past_indicator_values(self) -> None:
        config_path = Path(__file__).parents[1] / "config.demo.intraday.example.json"
        strategy = json.loads(config_path.read_text(encoding="utf-8"))["strategy"]
        candles = [
            candle(index, 100 + index * 0.1, 101 + index * 0.1, 99 + index * 0.1, 100.2 + index * 0.1)
            for index in range(300)
        ]
        current_index = 220
        partial = build_indicators(candles[: current_index + 1], strategy)
        complete = build_indicators(candles, strategy)
        for name in partial.__dataclass_fields__:
            self.assertEqual(getattr(partial, name)[current_index], getattr(complete, name)[current_index])


if __name__ == "__main__":
    unittest.main()
