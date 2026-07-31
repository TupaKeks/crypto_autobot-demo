from __future__ import annotations

import json
import unittest

from crypto_autobot.github_runner import contains_private_keys, sanitize_public_state


class GitHubRunnerTests(unittest.TestCase):
    def sample_state(self) -> dict:
        return {
            "updated_at": "2026-07-31T12:00:00+02:00",
            "mode": "demo",
            "orders_enabled": False,
            "broker_status": {
                "connected": True,
                "environment": "demo",
                "available_balance": 12345.67,
                "position_mode": "one-way",
            },
            "logs": [],
            "stats": {
                "balance": 12345.67,
                "initial_balance": 10000,
                "realized_pnl": 345.67,
                "return_percent": 3.46,
                "closed_trades": 4,
                "wins": 3,
                "losses": 1,
                "win_rate": 75,
                "open_positions": 1,
                "open_unrealized_pnl": 12.34,
            },
            "equity_now": 12358.01,
            "positions": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "entry": 65000,
                    "qty": 0.12345,
                    "stop": 64000,
                    "target": 67000,
                    "unrealized_pnl": 12.34,
                }
            },
            "latest": {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "price": 65100,
                    "time": "2026-07-31 08:00 UTC",
                    "status": "position open",
                }
            },
            "trades": [
                {
                    "time": "2026-07-31T10:00:00+02:00",
                    "event": "close",
                    "symbol": "ETHUSDT",
                    "side": "short",
                    "qty": 1.25,
                    "price": 3200,
                    "pnl": 45.5,
                    "balance": 12345.67,
                    "reason": "Binance position closed; realized=45.50, commission=1.25",
                }
            ],
        }

    def test_public_snapshot_excludes_private_account_values(self):
        payload = sanitize_public_state(self.sample_state())
        serialized = json.dumps(payload)

        self.assertFalse(contains_private_keys(payload))
        self.assertNotIn("12345.67", serialized)
        self.assertNotIn("0.12345", serialized)
        self.assertEqual(payload["stats"]["return_percent"], 3.46)
        self.assertEqual(payload["positions"][0]["symbol"], "BTCUSDT")

    def test_closed_trade_exposes_result_not_money_pnl(self):
        payload = sanitize_public_state(self.sample_state())

        self.assertEqual(payload["trades"][0]["result"], "win")
        self.assertEqual(payload["trades"][0]["reason"], "Binance position closed")
        self.assertNotIn("pnl", payload["trades"][0])
        self.assertNotIn("qty", payload["trades"][0])

    def test_diagnostic_redacts_environment_secrets(self):
        state = self.sample_state()
        state["broker_status"]["connected"] = False
        state["broker_status"]["message"] = "Invalid key demo-key-value"
        state["logs"] = [{"time": "now", "message": "connection error demo-key-value"}]

        from unittest.mock import patch

        with patch.dict("os.environ", {"BINANCE_DEMO_API_KEY": "demo-key-value"}):
            payload = sanitize_public_state(state)

        serialized = json.dumps(payload)
        self.assertNotIn("demo-key-value", serialized)
        self.assertIn("[redacted]", serialized)


if __name__ == "__main__":
    unittest.main()
