from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from crypto_autobot.bot import Candle
from crypto_autobot.orderflow_model import evaluate_orderflow_signal, model_status


class FakeProbabilityModel:
    def predict_proba(self, rows):
        score = 0.82 if float(rows[0][0]) > 0 else 0.31
        return np.asarray([[1.0 - score, score]])


class OrderflowModelTests(unittest.TestCase):
    def test_model_gate_and_direction_are_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "model.joblib"
            path.touch()
            future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)).isoformat()
            artifact = {
                "model": FakeProbabilityModel(),
                "enabled": True,
                "message": "ready",
                "symbols": ["BTCUSDT"],
                "threshold": 0.75,
                "quantile": 0.98,
                "expires_at": future,
            }
            candles = [Candle(i, 99, 101, 98, 100, 10, i + 1) for i in range(161)]
            rows = [[1.0] + [0.0] * 32]
            rows = [None] * 160 + rows
            atrs = [None] * 160 + [2.0]

            with patch("crypto_autobot.orderflow_model._load_artifact", return_value=artifact), patch(
                "crypto_autobot.orderflow_model.build_base_features", return_value=(rows, atrs)
            ):
                decision = evaluate_orderflow_signal(
                    {"enabled": True, "model_path": "model.joblib"},
                    root,
                    "BTCUSDT",
                    candles,
                    candles,
                )

            self.assertEqual(decision.side, "long")
            self.assertAlmostEqual(decision.score, 0.82)
            self.assertEqual(decision.atr, 2.0)

    def test_expired_model_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "model.joblib"
            path.touch()
            artifact = {
                "model": FakeProbabilityModel(),
                "enabled": True,
                "expires_at": "2020-01-01T00:00:00+00:00",
            }
            with patch("crypto_autobot.orderflow_model._load_artifact", return_value=artifact):
                status = model_status({"enabled": True, "model_path": "model.joblib"}, root)

            self.assertFalse(status["ready"])
            self.assertEqual(status["message"], "model expired")


if __name__ == "__main__":
    unittest.main()
