from __future__ import annotations

import unittest

from crypto_autobot.bot import Candle
from crypto_autobot.strategy_intraday import IndicatorSet, evaluate_regime_pullback_signal


class DirectionControlTests(unittest.TestCase):
    def test_allow_longs_false_suppresses_an_otherwise_valid_long_signal(self) -> None:
        count = 23
        candles = [
            Candle(index * 900_000, 100.0, 101.0, 99.0, 100.0, 1_000.0, index * 900_000 + 899_999)
            for index in range(count)
        ]
        candles[-1] = Candle(
            (count - 1) * 900_000,
            100.0,
            103.0,
            99.0,
            102.0,
            1_000.0,
            count * 900_000 - 1,
        )
        values = [100.0] * count
        regime_slow = [99.0] * count
        regime_slow[-1] = 100.0
        rsi = [40.0] * count
        rsi[-1] = 50.0
        indicators = IndicatorSet(
            entry_fast=[101.0] * count,
            entry_slow=values,
            regime_fast=[101.0] * count,
            regime_slow=regime_slow,
            atr=[1.0] * count,
            adx=[20.0] * count,
            rsi=rsi,
            volume_sma=[1_000.0] * count,
            band_basis=values,
            band_stddev=[1.0] * count,
        )
        strategy = {
            "entry_slow_ema": 1,
            "regime_slow_ema": 1,
            "regime_slope_bars": 1,
            "volume_sma_length": 1,
            "atr_length": 1,
            "adx_length": 1,
            "rsi_length": 1,
            "band_length": 1,
            "breakout_lookback": 1,
            "sweep_lookback": 1,
            "orderflow_lookback": 1,
            "min_atr_percent": 0.0,
            "max_atr_percent": 10.0,
            "min_adx": 0.0,
            "min_volume_factor": 0.0,
            "min_regime_slope_percent": 0.0,
            "pullback_lookback": 1,
            "long_pullback_rsi": 55.0,
            "short_pullback_rsi": 55.0,
            "min_confirmation_body_ratio": 0.1,
            "max_entry_extension_atr": 2.0,
            "long_trigger_rsi": 45.0,
            "short_trigger_rsi": 55.0,
            "allow_longs": False,
            "allow_shorts": True,
        }

        blocked = evaluate_regime_pullback_signal(candles, count - 1, strategy, indicators)
        self.assertIsNone(blocked.side)

        strategy["allow_longs"] = True
        allowed = evaluate_regime_pullback_signal(candles, count - 1, strategy, indicators)
        self.assertEqual(allowed.side, "long")


if __name__ == "__main__":
    unittest.main()
