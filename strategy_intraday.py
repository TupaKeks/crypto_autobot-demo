"""Closed-candle intraday strategies shared by live and backtest code."""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Protocol, Sequence


class CandleLike(Protocol):
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclasses.dataclass(frozen=True)
class SignalDecision:
    side: str | None
    status: str
    reason: str = ""
    strength: float = 0.0
    atr_value: float | None = None


@dataclasses.dataclass(frozen=True)
class IndicatorSet:
    entry_fast: list[float | None]
    entry_slow: list[float | None]
    regime_fast: list[float | None]
    regime_slow: list[float | None]
    atr: list[float | None]
    adx: list[float | None]
    rsi: list[float | None]
    volume_sma: list[float | None]
    band_basis: list[float | None]
    band_stddev: list[float | None]


def sma(values: Sequence[float], length: int) -> list[float | None]:
    result: list[float | None] = []
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= length:
            total -= values[index - length]
        result.append(total / length if index >= length - 1 else None)
    return result


def ema(values: Sequence[float], length: int) -> list[float | None]:
    result: list[float | None] = []
    alpha = 2.0 / (length + 1)
    current: float | None = None
    for index, value in enumerate(values):
        if index < length - 1:
            result.append(None)
            continue
        if current is None:
            current = sum(values[index - length + 1 : index + 1]) / length
        else:
            current = value * alpha + current * (1 - alpha)
        result.append(current)
    return result


def rolling_stddev(values: Sequence[float], length: int) -> list[float | None]:
    result: list[float | None] = []
    for index in range(len(values)):
        if index < length - 1:
            result.append(None)
            continue
        window = values[index - length + 1 : index + 1]
        average = sum(window) / length
        variance = sum((value - average) ** 2 for value in window) / length
        result.append(math.sqrt(variance))
    return result


def atr(candles: Sequence[CandleLike], length: int) -> list[float | None]:
    ranges: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        if previous_close is None:
            ranges.append(candle.high - candle.low)
        else:
            ranges.append(
                max(
                    candle.high - candle.low,
                    abs(candle.high - previous_close),
                    abs(candle.low - previous_close),
                )
            )
        previous_close = candle.close
    return sma(ranges, length)


def wilder_rma(values: Sequence[float], length: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < length:
        return result
    current = sum(values[:length]) / length
    result[length - 1] = current
    for index in range(length, len(values)):
        current = (current * (length - 1) + values[index]) / length
        result[index] = current
    return result


def adx(candles: Sequence[CandleLike], length: int) -> list[float | None]:
    if not candles:
        return []
    true_ranges = [0.0]
    plus_dm = [0.0]
    minus_dm = [0.0]
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        up_move = current.high - previous.high
        down_move = previous.low - current.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )

    tr_smoothed = wilder_rma(true_ranges, length)
    plus_smoothed = wilder_rma(plus_dm, length)
    minus_smoothed = wilder_rma(minus_dm, length)
    dx: list[float | None] = [None] * len(candles)
    for index in range(len(candles)):
        if tr_smoothed[index] is None or not tr_smoothed[index]:
            continue
        plus_di = 100 * float(plus_smoothed[index]) / float(tr_smoothed[index])
        minus_di = 100 * float(minus_smoothed[index]) / float(tr_smoothed[index])
        total = plus_di + minus_di
        if total:
            dx[index] = 100 * abs(plus_di - minus_di) / total

    result: list[float | None] = [None] * len(candles)
    valid = [(index, value) for index, value in enumerate(dx) if value is not None]
    if len(valid) < length:
        return result
    first_index = valid[length - 1][0]
    current_adx = sum(float(value) for _, value in valid[:length]) / length
    result[first_index] = current_adx
    for index, value in valid[length:]:
        current_adx = (current_adx * (length - 1) + float(value)) / length
        result[index] = current_adx
    return result


def rsi(values: Sequence[float], length: int) -> list[float | None]:
    if not values:
        return []
    gains = [0.0]
    losses = [0.0]
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gains = wilder_rma(gains, length)
    avg_losses = wilder_rma(losses, length)
    result: list[float | None] = [None] * len(values)
    for index in range(len(values)):
        if avg_gains[index] is None or avg_losses[index] is None:
            continue
        if float(avg_losses[index]) == 0:
            result[index] = 100.0
        else:
            relative = float(avg_gains[index]) / float(avg_losses[index])
            result[index] = 100 - 100 / (1 + relative)
    return result


def minimum_history(strategy: dict[str, Any]) -> int:
    return max(
        int(strategy["regime_slow_ema"]) + int(strategy.get("regime_slope_bars", 1)),
        int(strategy["entry_slow_ema"]) + int(strategy.get("pullback_lookback", 1)),
        int(strategy["volume_sma_length"]),
        int(strategy["atr_length"]) * 2 + 2,
        int(strategy["adx_length"]) * 3,
        int(strategy["rsi_length"]) + 2,
        int(strategy.get("band_length", 20)) + 2,
        int(strategy.get("breakout_lookback", 20)) + 2,
        int(strategy.get("sweep_lookback", 12)) + 2,
        int(strategy.get("orderflow_lookback", 3)) + 2,
    )


def taker_imbalance(candle: CandleLike) -> float | None:
    volume = float(getattr(candle, "volume", 0.0))
    taker_buy = float(getattr(candle, "taker_buy_volume", 0.0))
    if volume <= 0 or taker_buy <= 0:
        return None
    return max(-1.0, min(1.0, (2.0 * taker_buy - volume) / volume))


def build_indicators(candles: Sequence[CandleLike], strategy: dict[str, Any]) -> IndicatorSet:
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    band_length = int(strategy.get("band_length", 20))
    return IndicatorSet(
        entry_fast=ema(closes, int(strategy["entry_fast_ema"])),
        entry_slow=ema(closes, int(strategy["entry_slow_ema"])),
        regime_fast=ema(closes, int(strategy["regime_fast_ema"])),
        regime_slow=ema(closes, int(strategy["regime_slow_ema"])),
        atr=atr(candles, int(strategy["atr_length"])),
        adx=adx(candles, int(strategy["adx_length"])),
        rsi=rsi(closes, int(strategy["rsi_length"])),
        volume_sma=sma(volumes, int(strategy["volume_sma_length"])),
        band_basis=sma(closes, band_length),
        band_stddev=rolling_stddev(closes, band_length),
    )


def evaluate_intraday_signal(
    candles: Sequence[CandleLike],
    index: int,
    strategy: dict[str, Any],
    indicators: IndicatorSet | None = None,
) -> SignalDecision:
    if index < minimum_history(strategy):
        return SignalDecision(None, "indicators warming up")
    indicators = indicators or build_indicators(candles, strategy)
    candle = candles[index]
    previous = candles[index - 1]
    values = (
        indicators.entry_fast[index],
        indicators.entry_slow[index],
        indicators.regime_fast[index],
        indicators.regime_slow[index],
        indicators.atr[index],
        indicators.adx[index],
        indicators.rsi[index],
        indicators.volume_sma[index],
    )
    if any(value is None for value in values):
        return SignalDecision(None, "indicators warming up")

    entry_fast = float(indicators.entry_fast[index])
    entry_slow = float(indicators.entry_slow[index])
    regime_fast = float(indicators.regime_fast[index])
    regime_slow = float(indicators.regime_slow[index])
    atr_value = float(indicators.atr[index])
    adx_value = float(indicators.adx[index])
    rsi_value = float(indicators.rsi[index])
    volume_average = float(indicators.volume_sma[index])

    atr_percent = atr_value / candle.close * 100
    if not float(strategy["min_atr_percent"]) <= atr_percent <= float(strategy["max_atr_percent"]):
        return SignalDecision(None, f"ATR filter: {atr_percent:.2f}%", atr_value=atr_value)
    if adx_value < float(strategy["min_adx"]):
        return SignalDecision(None, f"ADX filter: {adx_value:.1f}", atr_value=atr_value)
    if candle.volume < volume_average * float(strategy["min_volume_factor"]):
        return SignalDecision(None, "volume filter", atr_value=atr_value)

    slope_bars = int(strategy["regime_slope_bars"])
    past_regime = indicators.regime_slow[index - slope_bars]
    if past_regime is None:
        return SignalDecision(None, "regime warming up", atr_value=atr_value)
    slope_percent = (regime_slow - float(past_regime)) / candle.close * 100
    min_slope = float(strategy["min_regime_slope_percent"])
    long_regime = regime_fast > regime_slow and candle.close > regime_slow and slope_percent >= min_slope
    short_regime = regime_fast < regime_slow and candle.close < regime_slow and slope_percent <= -min_slope

    pullback_bars = int(strategy["pullback_lookback"])
    tolerance = float(strategy["pullback_tolerance_atr"])
    long_pullback = False
    short_pullback = False
    for pullback_index in range(index - pullback_bars + 1, index + 1):
        slow_value = indicators.entry_slow[pullback_index]
        atr_at_pullback = indicators.atr[pullback_index]
        if slow_value is None or atr_at_pullback is None:
            continue
        long_pullback = long_pullback or (
            candles[pullback_index].low <= float(slow_value) + float(atr_at_pullback) * tolerance
        )
        short_pullback = short_pullback or (
            candles[pullback_index].high >= float(slow_value) - float(atr_at_pullback) * tolerance
        )

    candle_range = max(candle.high - candle.low, 1e-12)
    body_ratio = abs(candle.close - candle.open) / candle_range
    min_body = float(strategy["min_confirmation_body_ratio"])
    long_confirmation = (
        candle.close > candle.open
        and candle.close > previous.high
        and candle.close > entry_fast > entry_slow
        and body_ratio >= min_body
    )
    short_confirmation = (
        candle.close < candle.open
        and candle.close < previous.low
        and candle.close < entry_fast < entry_slow
        and body_ratio >= min_body
    )
    long_rsi = float(strategy["long_rsi_min"]) <= rsi_value <= float(strategy["long_rsi_max"])
    short_rsi = float(strategy["short_rsi_min"]) <= rsi_value <= float(strategy["short_rsi_max"])
    strength = adx_value + body_ratio * 20 + candle.volume / max(volume_average, 1e-12) * 5

    if bool(strategy.get("allow_longs", True)) and long_regime and long_pullback and long_confirmation and long_rsi:
        return SignalDecision(
            "long",
            "long signal",
            "trend pullback + bullish continuation",
            strength,
            atr_value,
        )
    if bool(strategy.get("allow_shorts", True)) and short_regime and short_pullback and short_confirmation and short_rsi:
        return SignalDecision(
            "short",
            "short signal",
            "trend pullback + bearish continuation",
            strength,
            atr_value,
        )
    return SignalDecision(None, "no signal", atr_value=atr_value)


def evaluate_mean_reversion_signal(
    candles: Sequence[CandleLike],
    index: int,
    strategy: dict[str, Any],
    indicators: IndicatorSet | None = None,
) -> SignalDecision:
    if index < minimum_history(strategy):
        return SignalDecision(None, "indicators warming up")
    indicators = indicators or build_indicators(candles, strategy)
    candle = candles[index]
    values = (
        indicators.regime_slow[index],
        indicators.atr[index],
        indicators.adx[index],
        indicators.rsi[index],
        indicators.band_basis[index],
        indicators.band_stddev[index],
    )
    if any(value is None for value in values):
        return SignalDecision(None, "indicators warming up")

    regime_slow = float(indicators.regime_slow[index])
    atr_value = float(indicators.atr[index])
    adx_value = float(indicators.adx[index])
    rsi_value = float(indicators.rsi[index])
    basis = float(indicators.band_basis[index])
    deviation = float(indicators.band_stddev[index])
    band_multiplier = float(strategy["band_stddev"])
    lower_band = basis - deviation * band_multiplier
    upper_band = basis + deviation * band_multiplier

    atr_percent = atr_value / candle.close * 100
    if not float(strategy["min_atr_percent"]) <= atr_percent <= float(strategy["max_atr_percent"]):
        return SignalDecision(None, f"ATR filter: {atr_percent:.2f}%", atr_value=atr_value)
    if adx_value > float(strategy["max_mean_reversion_adx"]):
        return SignalDecision(None, f"trend too strong: ADX {adx_value:.1f}", atr_value=atr_value)
    if abs(candle.close - regime_slow) > atr_value * float(strategy["max_distance_from_regime_atr"]):
        return SignalDecision(None, "too far from regime mean", atr_value=atr_value)

    slope_bars = int(strategy["regime_slope_bars"])
    past_regime = indicators.regime_slow[index - slope_bars]
    if past_regime is None:
        return SignalDecision(None, "regime warming up", atr_value=atr_value)
    slope_percent = abs(regime_slow - float(past_regime)) / candle.close * 100
    if slope_percent > float(strategy["max_mean_reversion_slope_percent"]):
        return SignalDecision(None, "regime slope filter", atr_value=atr_value)

    candle_range = max(candle.high - candle.low, 1e-12)
    body_ratio = abs(candle.close - candle.open) / candle_range
    min_body = float(strategy["min_confirmation_body_ratio"])
    long_reclaim = (
        candle.low < lower_band
        and candle.close > lower_band
        and candle.close > candle.open
        and body_ratio >= min_body
        and rsi_value <= float(strategy["mean_reversion_long_rsi_max"])
    )
    short_reclaim = (
        candle.high > upper_band
        and candle.close < upper_band
        and candle.close < candle.open
        and body_ratio >= min_body
        and rsi_value >= float(strategy["mean_reversion_short_rsi_min"])
    )
    penetration = (
        max(lower_band - candle.low, candle.high - upper_band, 0.0) / max(atr_value, 1e-12)
    )
    strength = (float(strategy["max_mean_reversion_adx"]) - adx_value) + body_ratio * 20 + penetration * 10
    if bool(strategy.get("allow_longs", True)) and long_reclaim:
        return SignalDecision(
            "long",
            "long mean-reversion signal",
            "lower volatility band reclaim",
            strength,
            atr_value,
        )
    if bool(strategy.get("allow_shorts", True)) and short_reclaim:
        return SignalDecision(
            "short",
            "short mean-reversion signal",
            "upper volatility band reclaim",
            strength,
            atr_value,
        )
    return SignalDecision(None, "no signal", atr_value=atr_value)


def evaluate_breakout_signal(
    candles: Sequence[CandleLike],
    index: int,
    strategy: dict[str, Any],
    indicators: IndicatorSet | None = None,
) -> SignalDecision:
    if index < minimum_history(strategy):
        return SignalDecision(None, "indicators warming up")
    indicators = indicators or build_indicators(candles, strategy)
    candle = candles[index]
    values = (
        indicators.entry_fast[index],
        indicators.entry_slow[index],
        indicators.regime_fast[index],
        indicators.regime_slow[index],
        indicators.atr[index],
        indicators.adx[index],
        indicators.rsi[index],
        indicators.volume_sma[index],
    )
    if any(value is None for value in values):
        return SignalDecision(None, "indicators warming up")

    entry_fast = float(indicators.entry_fast[index])
    entry_slow = float(indicators.entry_slow[index])
    regime_fast = float(indicators.regime_fast[index])
    regime_slow = float(indicators.regime_slow[index])
    atr_value = float(indicators.atr[index])
    adx_value = float(indicators.adx[index])
    rsi_value = float(indicators.rsi[index])
    volume_average = float(indicators.volume_sma[index])
    atr_percent = atr_value / candle.close * 100
    if not float(strategy["min_atr_percent"]) <= atr_percent <= float(strategy["max_atr_percent"]):
        return SignalDecision(None, f"ATR filter: {atr_percent:.2f}%", atr_value=atr_value)
    if adx_value < float(strategy["min_adx"]):
        return SignalDecision(None, f"ADX filter: {adx_value:.1f}", atr_value=atr_value)
    if candle.volume < volume_average * float(strategy["min_volume_factor"]):
        return SignalDecision(None, "volume filter", atr_value=atr_value)

    lookback = int(strategy["breakout_lookback"])
    window = candles[index - lookback : index]
    resistance = max(item.high for item in window)
    support = min(item.low for item in window)
    candle_range = max(candle.high - candle.low, 1e-12)
    body_ratio = abs(candle.close - candle.open) / candle_range
    if body_ratio < float(strategy["min_confirmation_body_ratio"]):
        return SignalDecision(None, "confirmation body filter", atr_value=atr_value)

    max_extension = atr_value * float(strategy["max_breakout_extension_atr"])
    long_breakout = resistance < candle.close <= resistance + max_extension
    short_breakout = support > candle.close >= support - max_extension
    long_regime = candle.close > entry_fast > entry_slow and regime_fast > regime_slow
    short_regime = candle.close < entry_fast < entry_slow and regime_fast < regime_slow
    long_rsi = float(strategy["long_rsi_min"]) <= rsi_value <= float(strategy["long_rsi_max"])
    short_rsi = float(strategy["short_rsi_min"]) <= rsi_value <= float(strategy["short_rsi_max"])
    volume_strength = candle.volume / max(volume_average, 1e-12)
    extension = max(candle.close - resistance, support - candle.close, 0.0) / max(atr_value, 1e-12)
    strength = adx_value + body_ratio * 20 + volume_strength * 5 - extension * 5

    if (
        bool(strategy.get("allow_longs", True))
        and long_breakout
        and long_regime
        and long_rsi
        and candle.close > candle.open
    ):
        return SignalDecision(
            "long",
            "long breakout signal",
            f"close above {lookback}-bar high",
            strength,
            atr_value,
        )
    if (
        bool(strategy.get("allow_shorts", True))
        and short_breakout
        and short_regime
        and short_rsi
        and candle.close < candle.open
    ):
        return SignalDecision(
            "short",
            "short breakout signal",
            f"close below {lookback}-bar low",
            strength,
            atr_value,
        )
    return SignalDecision(None, "no signal", atr_value=atr_value)


def evaluate_regime_pullback_signal(
    candles: Sequence[CandleLike],
    index: int,
    strategy: dict[str, Any],
    indicators: IndicatorSet | None = None,
) -> SignalDecision:
    if index < minimum_history(strategy):
        return SignalDecision(None, "indicators warming up")
    indicators = indicators or build_indicators(candles, strategy)
    candle = candles[index]
    previous_rsi = indicators.rsi[index - 1]
    values = (
        indicators.entry_fast[index],
        indicators.entry_slow[index],
        indicators.regime_fast[index],
        indicators.regime_slow[index],
        indicators.atr[index],
        indicators.adx[index],
        indicators.rsi[index],
        indicators.volume_sma[index],
        previous_rsi,
    )
    if any(value is None for value in values):
        return SignalDecision(None, "indicators warming up")

    entry_fast = float(indicators.entry_fast[index])
    entry_slow = float(indicators.entry_slow[index])
    regime_fast = float(indicators.regime_fast[index])
    regime_slow = float(indicators.regime_slow[index])
    atr_value = float(indicators.atr[index])
    adx_value = float(indicators.adx[index])
    rsi_value = float(indicators.rsi[index])
    volume_average = float(indicators.volume_sma[index])
    atr_percent = atr_value / candle.close * 100
    if not float(strategy["min_atr_percent"]) <= atr_percent <= float(strategy["max_atr_percent"]):
        return SignalDecision(None, f"ATR filter: {atr_percent:.2f}%", atr_value=atr_value)
    if adx_value < float(strategy["min_adx"]):
        return SignalDecision(None, f"ADX filter: {adx_value:.1f}", atr_value=atr_value)
    if candle.volume < volume_average * float(strategy["min_volume_factor"]):
        return SignalDecision(None, "volume filter", atr_value=atr_value)

    slope_bars = int(strategy["regime_slope_bars"])
    past_regime = indicators.regime_slow[index - slope_bars]
    if past_regime is None:
        return SignalDecision(None, "regime warming up", atr_value=atr_value)
    slope_percent = (regime_slow - float(past_regime)) / candle.close * 100
    min_slope = float(strategy["min_regime_slope_percent"])
    long_regime = regime_fast > regime_slow and candle.close > regime_slow and slope_percent >= min_slope
    short_regime = regime_fast < regime_slow and candle.close < regime_slow and slope_percent <= -min_slope

    lookback = int(strategy["pullback_lookback"])
    start = index - lookback + 1
    long_pullback = False
    short_pullback = False
    for pullback_index in range(start, index + 1):
        slow_value = indicators.entry_slow[pullback_index]
        pullback_rsi = indicators.rsi[pullback_index]
        if slow_value is None or pullback_rsi is None:
            continue
        long_pullback = long_pullback or (
            candles[pullback_index].low <= float(slow_value)
            and float(pullback_rsi) <= float(strategy["long_pullback_rsi"])
        )
        short_pullback = short_pullback or (
            candles[pullback_index].high >= float(slow_value)
            and float(pullback_rsi) >= float(strategy["short_pullback_rsi"])
        )

    candle_range = max(candle.high - candle.low, 1e-12)
    body_ratio = abs(candle.close - candle.open) / candle_range
    confirmation = body_ratio >= float(strategy["min_confirmation_body_ratio"])
    extension = abs(candle.close - entry_fast) / max(atr_value, 1e-12)
    if extension > float(strategy["max_entry_extension_atr"]):
        return SignalDecision(None, "entry extension filter", atr_value=atr_value)

    long_trigger = (
        float(previous_rsi) < float(strategy["long_trigger_rsi"]) <= rsi_value
        and candle.close > entry_fast
        and candle.close > candle.open
        and confirmation
    )
    short_trigger = (
        float(previous_rsi) > float(strategy["short_trigger_rsi"]) >= rsi_value
        and candle.close < entry_fast
        and candle.close < candle.open
        and confirmation
    )
    strength = adx_value + body_ratio * 20 - extension * 5
    if bool(strategy.get("allow_longs", True)) and long_regime and long_pullback and long_trigger:
        return SignalDecision(
            "long",
            "long regime pullback signal",
            "trend-aligned RSI recovery",
            strength,
            atr_value,
        )
    if bool(strategy.get("allow_shorts", True)) and short_regime and short_pullback and short_trigger:
        return SignalDecision(
            "short",
            "short regime pullback signal",
            "trend-aligned RSI rollover",
            strength,
            atr_value,
        )
    return SignalDecision(None, "no signal", atr_value=atr_value)


def evaluate_orderflow_pullback_signal(
    candles: Sequence[CandleLike],
    index: int,
    strategy: dict[str, Any],
    indicators: IndicatorSet | None = None,
) -> SignalDecision:
    """Trend pullback confirmed by futures taker buy/sell imbalance."""
    if index < minimum_history(strategy):
        return SignalDecision(None, "indicators warming up")
    indicators = indicators or build_indicators(candles, strategy)
    candle = candles[index]
    previous_rsi = indicators.rsi[index - 1]
    values = (
        indicators.entry_fast[index],
        indicators.entry_slow[index],
        indicators.regime_fast[index],
        indicators.regime_slow[index],
        indicators.atr[index],
        indicators.adx[index],
        indicators.rsi[index],
        indicators.volume_sma[index],
        previous_rsi,
    )
    if any(value is None for value in values):
        return SignalDecision(None, "indicators warming up")

    flow_lookback = int(strategy.get("orderflow_lookback", 3))
    flow_window = [taker_imbalance(item) for item in candles[index - flow_lookback + 1 : index + 1]]
    if any(value is None for value in flow_window):
        return SignalDecision(None, "orderflow unavailable")
    current_flow = float(flow_window[-1])
    average_flow = sum(float(value) for value in flow_window) / len(flow_window)
    current_threshold = float(strategy.get("min_current_orderflow", 0.0))
    average_threshold = float(strategy.get("min_average_orderflow", 0.0))
    max_current = float(strategy.get("max_current_orderflow", 1.0))
    max_average = float(strategy.get("max_average_orderflow", 1.0))

    entry_fast = float(indicators.entry_fast[index])
    entry_slow = float(indicators.entry_slow[index])
    regime_fast = float(indicators.regime_fast[index])
    regime_slow = float(indicators.regime_slow[index])
    atr_value = float(indicators.atr[index])
    adx_value = float(indicators.adx[index])
    rsi_value = float(indicators.rsi[index])
    volume_average = float(indicators.volume_sma[index])
    atr_percent = atr_value / candle.close * 100
    if not float(strategy["min_atr_percent"]) <= atr_percent <= float(strategy["max_atr_percent"]):
        return SignalDecision(None, f"ATR filter: {atr_percent:.2f}%", atr_value=atr_value)
    if adx_value < float(strategy["min_adx"]):
        return SignalDecision(None, f"ADX filter: {adx_value:.1f}", atr_value=atr_value)
    if candle.volume < volume_average * float(strategy["min_volume_factor"]):
        return SignalDecision(None, "volume filter", atr_value=atr_value)

    slope_bars = int(strategy["regime_slope_bars"])
    past_regime = indicators.regime_slow[index - slope_bars]
    if past_regime is None:
        return SignalDecision(None, "regime warming up", atr_value=atr_value)
    slope_percent = (regime_slow - float(past_regime)) / candle.close * 100
    min_slope = float(strategy["min_regime_slope_percent"])
    long_regime = regime_fast > regime_slow and candle.close > regime_slow and slope_percent >= min_slope
    short_regime = regime_fast < regime_slow and candle.close < regime_slow and slope_percent <= -min_slope

    pullback_lookback = int(strategy["pullback_lookback"])
    long_pullback = False
    short_pullback = False
    for pullback_index in range(index - pullback_lookback + 1, index + 1):
        slow_value = indicators.entry_slow[pullback_index]
        pullback_rsi = indicators.rsi[pullback_index]
        if slow_value is None or pullback_rsi is None:
            continue
        long_pullback = long_pullback or (
            candles[pullback_index].low <= float(slow_value)
            and float(pullback_rsi) <= float(strategy["long_pullback_rsi"])
        )
        short_pullback = short_pullback or (
            candles[pullback_index].high >= float(slow_value)
            and float(pullback_rsi) >= float(strategy["short_pullback_rsi"])
        )

    candle_range = max(candle.high - candle.low, 1e-12)
    body_ratio = abs(candle.close - candle.open) / candle_range
    confirmation = body_ratio >= float(strategy["min_confirmation_body_ratio"])
    extension = abs(candle.close - entry_fast) / max(atr_value, 1e-12)
    if extension > float(strategy["max_entry_extension_atr"]):
        return SignalDecision(None, "entry extension filter", atr_value=atr_value)

    long_trigger = (
        float(previous_rsi) < float(strategy["long_trigger_rsi"]) <= rsi_value
        and candle.close > entry_fast
        and candle.close > candle.open
        and confirmation
    )
    short_trigger = (
        float(previous_rsi) > float(strategy["short_trigger_rsi"]) >= rsi_value
        and candle.close < entry_fast
        and candle.close < candle.open
        and confirmation
    )
    long_flow = (
        current_threshold <= current_flow <= max_current
        and average_threshold <= average_flow <= max_average
    )
    short_flow = (
        -max_current <= current_flow <= -current_threshold
        and -max_average <= average_flow <= -average_threshold
    )
    target_flow = (current_threshold + max_current) / 2
    flow_quality = max(0.0, 1.0 - abs(abs(current_flow) - target_flow) / max(max_current, 1e-12))
    strength = adx_value + body_ratio * 20 - extension * 5 + flow_quality * 15
    if (
        bool(strategy.get("allow_longs", True))
        and long_regime
        and long_pullback
        and long_trigger
        and long_flow
    ):
        return SignalDecision(
            "long",
            "long orderflow pullback signal",
            "trend-aligned RSI recovery + taker buying",
            strength,
            atr_value,
        )
    if (
        bool(strategy.get("allow_shorts", True))
        and short_regime
        and short_pullback
        and short_trigger
        and short_flow
    ):
        return SignalDecision(
            "short",
            "short orderflow pullback signal",
            "trend-aligned RSI rollover + taker selling",
            strength,
            atr_value,
        )
    return SignalDecision(None, "no signal", atr_value=atr_value)


def evaluate_liquidity_sweep_signal(
    candles: Sequence[CandleLike],
    index: int,
    strategy: dict[str, Any],
    indicators: IndicatorSet | None = None,
) -> SignalDecision:
    """Trade a closed-candle rejection after taking a recent range extreme."""
    if index < minimum_history(strategy):
        return SignalDecision(None, "indicators warming up")
    indicators = indicators or build_indicators(candles, strategy)
    candle = candles[index]
    confirmation_bars = int(strategy.get("sweep_confirmation_bars", 0))
    sweep_index = index - confirmation_bars
    if sweep_index <= 0:
        return SignalDecision(None, "sweep history warming up")
    sweep_candle = candles[sweep_index]
    values = (
        indicators.regime_fast[index],
        indicators.regime_slow[index],
        indicators.atr[index],
        indicators.adx[index],
        indicators.rsi[index],
        indicators.volume_sma[index],
    )
    if any(value is None for value in values):
        return SignalDecision(None, "indicators warming up")

    atr_value = float(indicators.atr[index])
    atr_percent = atr_value / candle.close * 100
    if not float(strategy["min_atr_percent"]) <= atr_percent <= float(strategy["max_atr_percent"]):
        return SignalDecision(None, f"ATR filter: {atr_percent:.2f}%", atr_value=atr_value)
    adx_value = float(indicators.adx[index])
    if adx_value < float(strategy.get("min_adx", 0.0)):
        return SignalDecision(None, f"ADX filter: {adx_value:.1f}", atr_value=atr_value)
    volume_average = float(indicators.volume_sma[index])
    if candle.volume < volume_average * float(strategy.get("min_volume_factor", 0.0)):
        return SignalDecision(None, "volume filter", atr_value=atr_value)

    lookback = int(strategy.get("sweep_lookback", 12))
    prior = candles[sweep_index - lookback : sweep_index]
    prior_low = min(item.low for item in prior)
    prior_high = max(item.high for item in prior)
    penetration = atr_value * float(strategy.get("min_sweep_atr", 0.05))
    sweep_range = max(sweep_candle.high - sweep_candle.low, 1e-12)
    lower_wick = min(sweep_candle.open, sweep_candle.close) - sweep_candle.low
    upper_wick = sweep_candle.high - max(sweep_candle.open, sweep_candle.close)
    minimum_wick = float(strategy.get("min_sweep_wick_ratio", 0.25))
    bullish_rejection = (
        sweep_candle.low < prior_low - penetration
        and sweep_candle.close > prior_low
        and lower_wick / sweep_range >= minimum_wick
    )
    bearish_rejection = (
        sweep_candle.high > prior_high + penetration
        and sweep_candle.close < prior_high
        and upper_wick / sweep_range >= minimum_wick
    )
    if confirmation_bars:
        body_high = max(sweep_candle.open, sweep_candle.close)
        body_low = min(sweep_candle.open, sweep_candle.close)
        confirmation_range = max(candle.high - candle.low, 1e-12)
        body_ratio = abs(candle.close - candle.open) / confirmation_range
        minimum_body = float(strategy.get("sweep_confirmation_body_ratio", 0.2))
        bullish_rejection = (
            bullish_rejection
            and candle.close > body_high
            and candle.close > candle.open
            and body_ratio >= minimum_body
        )
        bearish_rejection = (
            bearish_rejection
            and candle.close < body_low
            and candle.close < candle.open
            and body_ratio >= minimum_body
        )
    else:
        bullish_rejection = bullish_rejection and candle.close > candle.open
        bearish_rejection = bearish_rejection and candle.close < candle.open

    regime_mode = str(strategy.get("sweep_regime", "none"))
    regime_fast = float(indicators.regime_fast[index])
    regime_slow = float(indicators.regime_slow[index])
    if regime_mode == "aligned":
        bullish_rejection = bullish_rejection and regime_fast >= regime_slow
        bearish_rejection = bearish_rejection and regime_fast <= regime_slow
    elif regime_mode == "counter":
        bullish_rejection = bullish_rejection and regime_fast < regime_slow
        bearish_rejection = bearish_rejection and regime_fast > regime_slow

    rsi_value = float(indicators.rsi[index])
    bullish_rejection = bullish_rejection and rsi_value <= float(strategy.get("sweep_long_rsi_max", 100.0))
    bearish_rejection = bearish_rejection and rsi_value >= float(strategy.get("sweep_short_rsi_min", 0.0))
    sweep_depth = max(
        prior_low - sweep_candle.low,
        sweep_candle.high - prior_high,
        0.0,
    ) / max(atr_value, 1e-12)
    wick_strength = max(lower_wick, upper_wick) / sweep_range
    strength = sweep_depth * 20 + wick_strength * 10 + adx_value * 0.1
    if bool(strategy.get("allow_longs", True)) and bullish_rejection:
        return SignalDecision("long", "bullish liquidity sweep", "low swept and reclaimed", strength, atr_value)
    if bool(strategy.get("allow_shorts", True)) and bearish_rejection:
        return SignalDecision("short", "bearish liquidity sweep", "high swept and rejected", strength, atr_value)
    return SignalDecision(None, "no signal", atr_value=atr_value)


def evaluate_strategy_signal(
    candles: Sequence[CandleLike],
    index: int,
    strategy: dict[str, Any],
    indicators: IndicatorSet | None = None,
) -> SignalDecision:
    strategy_type = str(strategy.get("type", "intraday_pullback"))
    if strategy_type == "intraday_mean_reversion":
        return evaluate_mean_reversion_signal(candles, index, strategy, indicators)
    if strategy_type == "intraday_breakout":
        return evaluate_breakout_signal(candles, index, strategy, indicators)
    if strategy_type == "intraday_regime_pullback":
        return evaluate_regime_pullback_signal(candles, index, strategy, indicators)
    if strategy_type == "intraday_orderflow_pullback":
        return evaluate_orderflow_pullback_signal(candles, index, strategy, indicators)
    if strategy_type == "intraday_liquidity_sweep":
        return evaluate_liquidity_sweep_signal(candles, index, strategy, indicators)
    return evaluate_intraday_signal(candles, index, strategy, indicators)
