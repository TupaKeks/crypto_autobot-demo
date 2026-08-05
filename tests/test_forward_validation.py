from __future__ import annotations

import datetime as dt
import unittest

from crypto_autobot.forward_validation import forward_validation_report


def validation_config() -> dict:
    return {
        "market": {"interval": "15m", "symbols": ["BTCUSDT"]},
        "strategy": {"stop_atr": 2.0, "target_atr": 3.2},
        "forward_validation": {
            "min_observation_days": 30,
            "min_closed_trades": 100,
            "min_trades_per_day": 3,
            "max_trades_per_day": 7,
            "min_win_rate_percent": 45,
            "min_profit_factor": 1.1,
            "max_drawdown_percent": 10,
            "min_return_percent": 0,
            "min_nominal_reward_risk": 1.5,
            "min_daily_data_coverage_percent": 75,
        },
    }


class ForwardValidationTests(unittest.TestCase):
    def test_empty_coverage_does_not_fall_back_to_calendar_age(self):
        state = {
            "created_at": "2025-01-01T00:00:00+00:00",
            "validation_coverage": {},
            "initial_balance": 1000,
            "realized_pnl": 0,
            "trades": [],
        }

        report = forward_validation_report(
            validation_config(),
            state,
            now=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(report["observation_days"], 0)
        self.assertEqual(report["daily_coverage_required"], 72)

    def test_observation_day_requires_coverage_and_a_completed_date(self):
        state = {
            "created_at": "2026-07-01T00:00:00+00:00",
            "validation_active_dates": ["2026-07-01", "2026-07-02", "2026-08-01"],
            "validation_coverage": {
                "2026-07-01": {"symbol_candles": 72},
                "2026-07-02": {"symbol_candles": 71},
                "2026-08-01": {"symbol_candles": 96},
            },
            "initial_balance": 1000,
            "realized_pnl": 0,
            "trades": [],
        }

        report = forward_validation_report(
            validation_config(),
            state,
            now=dt.datetime(2026, 8, 1, 23, 59, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(report["daily_coverage_required"], 72)
        self.assertEqual(report["observation_days"], 1)
        self.assertEqual(report["qualified_observation_dates"], ["2026-07-01"])
        self.assertEqual(report["current_date"], "2026-08-01")
        self.assertEqual(report["current_date_coverage"], 96)
        self.assertEqual(report["confidence"]["trades_per_day_95"]["count"], 1)
        self.assertEqual(report["confidence"]["trades_per_day_95"]["mean"], 0.0)
        self.assertIn("Покрытие сегодня: 96/72", report["summary"])

    def test_gate_uses_only_durable_metrics_from_qualified_days(self):
        dates = [f"2026-07-{day:02d}" for day in range(1, 31)]
        state = {
            "created_at": "2026-07-01T00:00:00+00:00",
            "validation_coverage": {
                **{date_key: {"symbol_candles": 72} for date_key in dates},
                "2026-08-01": {"symbol_candles": 96},
            },
            "validation_daily_version": 1,
            "daily": {
                **{
                    date_key: {
                        "trades": 5,
                        "validation_trades": 5,
                        "validation_pnls": [3.2, -2.0, 3.2, -2.0, 3.2],
                        "validation_realized_rs": [1.5, -1.0, 1.5, -1.0, 1.5],
                    }
                    for date_key in dates
                },
                "2026-08-01": {
                    "trades": 12,
                    "validation_trades": 12,
                    "validation_pnls": [100.0] * 12,
                },
            },
            "initial_balance": 1000,
            "trades": [
                {"time": "2026-08-01T10:00:00+00:00", "event": "close", "pnl": 100.0}
            ],
        }

        report = forward_validation_report(
            validation_config(),
            state,
            now=dt.datetime(2026, 8, 1, 23, 59, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(report["observation_days"], 30)
        self.assertEqual(report["opened_trades"], 150)
        self.assertEqual(report["closed_trades"], 150)
        self.assertEqual(report["trades_per_day"], 5)
        self.assertEqual(report["win_rate"], 60)
        self.assertEqual(report["profit_factor"], 2.4)
        self.assertEqual(report["confidence"]["realized_r_coverage"], 150)
        self.assertGreater(report["confidence"]["mean_realized_r_95"]["lower"], 0)
        self.assertTrue(report["ready_for_live"])

    def test_manual_demo_test_is_excluded_from_live_gate(self):
        state = {
            "created_at": "2026-07-01T00:00:00+00:00",
            "validation_active_dates": [f"2026-07-{day:02d}" for day in range(1, 31)],
            "daily": {
                "2026-07-01": {"trades": 1, "validation_trades": 0},
            },
            "initial_balance": 1000,
            "realized_pnl": 100,
            "trades": [
                {"event": "open", "source": "manual_demo_test", "pnl": 0},
                {"event": "close", "source": "manual_demo_test", "pnl": 100},
            ],
        }

        report = forward_validation_report(
            validation_config(),
            state,
            now=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(report["opened_trades"], 0)
        self.assertEqual(report["closed_trades"], 0)
        self.assertEqual(report["return_percent"], 0)
        self.assertFalse(report["ready_for_live"])

    def test_empty_demo_state_is_collecting(self):
        state = {
            "created_at": "2026-08-01T00:00:00+00:00",
            "initial_balance": 1000,
            "realized_pnl": 0,
            "trades": [],
        }
        report = forward_validation_report(
            validation_config(),
            state,
            now=dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(report["status"], "collecting")
        self.assertFalse(report["ready_for_live"])
        self.assertEqual(report["closed_trades"], 0)

    def test_profitable_representative_sample_passes(self):
        trades = []
        pnl_values = [3.2] * 50 + [-2.0] * 50
        for index, pnl in enumerate(pnl_values):
            trades.append({"event": "open", "pnl": 0, "time": f"open-{index}"})
            trades.append({"event": "close", "pnl": pnl, "time": f"close-{index}"})
        state = {
            "created_at": "2026-07-01T00:00:00+00:00",
            "initial_balance": 1000,
            "realized_pnl": sum(pnl_values),
            "trades": trades,
        }
        report = forward_validation_report(
            validation_config(),
            state,
            now=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )
        self.assertTrue(report["ready_for_live"])
        self.assertEqual(report["win_rate"], 50.0)
        self.assertEqual(report["profit_factor"], 1.6)
        self.assertTrue(report["confidence"]["positive_expectancy_supported"])
        self.assertAlmostEqual(report["confidence"]["win_rate_95_percent"]["lower"], 40.38, places=2)

    def test_point_profit_is_rejected_when_expectancy_interval_crosses_zero(self):
        pnl_values = [1.0] * 55 + [-1.0] * 45
        trades = []
        for index, pnl in enumerate(pnl_values):
            trades.append({"event": "open", "pnl": 0, "time": f"open-{index}"})
            trades.append({"event": "close", "pnl": pnl, "time": f"close-{index}"})
        state = {
            "created_at": "2026-07-01T00:00:00+00:00",
            "validation_active_dates": [f"2026-07-{day:02d}" for day in range(1, 31)],
            "initial_balance": 1000,
            "trades": trades,
        }

        report = forward_validation_report(
            validation_config(),
            state,
            now=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )
        checks = {item["id"]: item for item in report["checks"]}

        self.assertTrue(checks["win_rate"]["passed"])
        self.assertTrue(checks["profit_factor"]["passed"])
        self.assertFalse(checks["expectancy_confidence"]["passed"])
        self.assertLess(report["confidence"]["mean_pnl_95"]["lower"], 0)
        self.assertFalse(report["ready_for_live"])

    def test_frequency_and_drawdown_can_reject_profitable_sample(self):
        trades = []
        pnl_values = [20.0] * 60 + [-18.0] * 40
        for index, pnl in enumerate(pnl_values):
            trades.append({"event": "open", "pnl": 0})
            trades.append({"event": "close", "pnl": pnl})
        state = {
            "created_at": "2026-05-01T00:00:00+00:00",
            "initial_balance": 1000,
            "realized_pnl": sum(pnl_values),
            "trades": trades,
        }
        report = forward_validation_report(
            validation_config(),
            state,
            now=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )
        checks = {item["id"]: item for item in report["checks"]}
        self.assertFalse(report["ready_for_live"])
        self.assertFalse(checks["trades_per_day"]["passed"])
        self.assertFalse(checks["max_drawdown"]["passed"])

    def test_active_dates_and_daily_totals_survive_trimmed_trade_log(self):
        pnl_values = [3.2, -2.0] * 50
        state = {
            "created_at": "2025-01-01T00:00:00+00:00",
            "validation_active_dates": [f"2026-07-{day:02d}" for day in range(1, 31)],
            "daily": {
                f"2026-07-{day:02d}": {"trades": 5}
                for day in range(1, 31)
            },
            "initial_balance": 1000,
            "realized_pnl": sum(pnl_values),
            "trades": [
                {"event": "close", "pnl": pnl}
                for pnl in pnl_values
            ],
        }
        report = forward_validation_report(
            validation_config(),
            state,
            now=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(report["observation_days"], 30.0)
        self.assertEqual(report["opened_trades"], 150)
        self.assertEqual(report["trades_per_day"], 5.0)
        self.assertTrue(report["ready_for_live"])


if __name__ == "__main__":
    unittest.main()
