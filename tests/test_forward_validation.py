from __future__ import annotations

import datetime as dt
import unittest

from crypto_autobot.forward_validation import forward_validation_report


def validation_config() -> dict:
    return {
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
        },
    }


class ForwardValidationTests(unittest.TestCase):
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
