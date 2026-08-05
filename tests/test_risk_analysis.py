from __future__ import annotations

import unittest

from crypto_autobot.risk_analysis import (
    DAY_MS,
    block_bootstrap_risk,
    daily_fractional_returns,
    max_consecutive_losses,
    path_metrics,
    rolling_window_risk,
)


class RiskAnalysisTests(unittest.TestCase):
    def test_daily_returns_use_side_specific_risk(self):
        trades = [
            {"entry_time": 0, "side": "long", "realized_r": 1.5},
            {"entry_time": DAY_MS + 1, "side": "short", "realized_r": -1.0},
            {"entry_time": 3 * DAY_MS, "side": "short", "realized_r": 99.0},
        ]

        returns = daily_fractional_returns(
            trades,
            0,
            3 * DAY_MS,
            {"long": 0.1, "short": 0.2},
        )

        self.assertEqual(len(returns), 3)
        self.assertAlmostEqual(returns[0], 0.0015)
        self.assertAlmostEqual(returns[1], -0.002)
        self.assertEqual(returns[2], 0.0)

    def test_path_metrics_compound_and_measure_drawdown(self):
        metrics = path_metrics([0.10, -0.10, 0.05])

        self.assertAlmostEqual(metrics["return_percent"], 3.95)
        self.assertAlmostEqual(metrics["max_drawdown_percent"], 10.0)

    def test_rolling_windows_do_not_pad_short_history(self):
        report = rolling_window_risk([0.01, -0.01, 0.01], horizon_days=2)

        self.assertEqual(report["paths"], 2)
        self.assertEqual(report["probability_profitable_percent"], 0.0)

    def test_block_bootstrap_is_deterministic(self):
        first = block_bootstrap_risk(
            [0.01, -0.02, 0.03, -0.01, 0.0],
            horizon_days=4,
            block_days=2,
            simulations=100,
            seed=7,
        )
        second = block_bootstrap_risk(
            [0.01, -0.02, 0.03, -0.01, 0.0],
            horizon_days=4,
            block_days=2,
            simulations=100,
            seed=7,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["paths"], 100)

    def test_max_consecutive_losses_uses_exit_order(self):
        trades = [
            {"exit_time": 3, "net_pnl": 1},
            {"exit_time": 1, "net_pnl": -1},
            {"exit_time": 2, "net_pnl": -2},
            {"exit_time": 4, "net_pnl": -1},
        ]

        self.assertEqual(max_consecutive_losses(trades), 2)


if __name__ == "__main__":
    unittest.main()
