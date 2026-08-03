"""Shared order-flow features for research and live model inference."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

try:
    from .strategy_intraday import adx, atr, ema, rsi, sma, taker_imbalance
except ImportError:
    from strategy_intraday import adx, atr, ema, rsi, sma, taker_imbalance


SIGNED_FEATURES = 21
UNSIGNED_FEATURES = 11
MINIMUM_HISTORY = 161


def log_return(current: float, previous: float) -> float:
    return math.log(current / previous) if current > 0 and previous > 0 else 0.0


def build_base_features(
    candles: list[Any],
    btc_by_time: dict[int, Any],
    symbol_index: int,
    symbol_count: int,
) -> tuple[list[list[float] | None], list[float | None]]:
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    trade_counts = [float(candle.trade_count) for candle in candles]
    imbalances = [float(taker_imbalance(candle) or 0.0) for candle in candles]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    ema48 = ema(closes, 48)
    ema144 = ema(closes, 144)
    atr14 = atr(candles, 14)
    rsi14 = rsi(closes, 14)
    adx14 = adx(candles, 14)
    volume20 = sma(volumes, 20)
    trades20 = sma(trade_counts, 20)
    imbalance3 = sma(imbalances, 3)
    imbalance12 = sma(imbalances, 12)

    btc_times = sorted(btc_by_time)
    btc_candles = [btc_by_time[timestamp] for timestamp in btc_times]
    btc_index = {timestamp: index for index, timestamp in enumerate(btc_times)}
    btc_closes = [candle.close for candle in btc_candles]
    btc_imbalances = [float(taker_imbalance(candle) or 0.0) for candle in btc_candles]
    btc_ema9 = ema(btc_closes, 9)
    btc_ema21 = ema(btc_closes, 21)
    btc_atr14 = atr(btc_candles, 14)
    btc_flow3 = sma(btc_imbalances, 3)

    rows: list[list[float] | None] = [None] * len(candles)
    for index, candle in enumerate(candles):
        if index < MINIMUM_HISTORY - 1:
            continue
        btc_i = btc_index.get(candle.open_time)
        values = (
            ema9[index], ema21[index], ema48[index], ema144[index], atr14[index],
            rsi14[index], adx14[index], volume20[index], trades20[index],
            imbalance3[index], imbalance12[index],
        )
        if btc_i is None or btc_i < 32 or any(value is None for value in values):
            continue
        if btc_ema9[btc_i] is None or btc_ema21[btc_i] is None or btc_atr14[btc_i] in (None, 0):
            continue
        atr_value = float(atr14[index])
        if atr_value <= 0 or float(volume20[index]) <= 0 or float(trades20[index]) <= 0:
            continue
        candle_range = max(candle.high - candle.low, 1e-12)
        timestamp = dt.datetime.fromtimestamp(candle.open_time / 1000, dt.timezone.utc)
        signed = [
            log_return(closes[index], closes[index - 1]),
            log_return(closes[index], closes[index - 2]),
            log_return(closes[index], closes[index - 4]),
            log_return(closes[index], closes[index - 8]),
            log_return(closes[index], closes[index - 16]),
            log_return(closes[index], closes[index - 32]),
            (candle.close - float(ema9[index])) / atr_value,
            (float(ema9[index]) - float(ema21[index])) / atr_value,
            (float(ema48[index]) - float(ema144[index])) / atr_value,
            float(rsi14[index]) / 100.0 - 0.5,
            (candle.close - candle.open) / candle_range,
            imbalances[index],
            float(imbalance3[index]),
            float(imbalance12[index]),
            imbalances[index] - imbalances[index - 1],
            log_return(btc_closes[btc_i], btc_closes[btc_i - 1]),
            log_return(btc_closes[btc_i], btc_closes[btc_i - 4]),
            log_return(btc_closes[btc_i], btc_closes[btc_i - 16]),
            (float(btc_ema9[btc_i]) - float(btc_ema21[btc_i])) / float(btc_atr14[btc_i]),
            btc_imbalances[btc_i],
            float(btc_flow3[btc_i] or 0.0),
        ]
        unsigned = [
            atr_value / candle.close,
            float(adx14[index]) / 100.0,
            min(candle.volume / float(volume20[index]), 10.0),
            min(candle.trade_count / float(trades20[index]), 10.0),
            abs(imbalances[index]),
            abs(float(imbalance3[index])),
            (candle.high - candle.low) / atr_value,
            math.sin(timestamp.hour * 2 * math.pi / 24),
            math.cos(timestamp.hour * 2 * math.pi / 24),
            math.sin(timestamp.weekday() * 2 * math.pi / 7),
            math.cos(timestamp.weekday() * 2 * math.pi / 7),
        ]
        one_hot = [1.0 if item == symbol_index else 0.0 for item in range(symbol_count)]
        rows[index] = signed + unsigned + one_hot
    return rows, atr14


def directional_features(base: list[float], direction: int, symbol_count: int) -> list[float]:
    return (
        [direction * value for value in base[:SIGNED_FEATURES]]
        + base[SIGNED_FEATURES : SIGNED_FEATURES + UNSIGNED_FEATURES]
        + base[-symbol_count:]
    )
