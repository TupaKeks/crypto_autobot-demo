#!/usr/bin/env python3
"""Train and calibrate the deployable 15-minute order-flow model."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import joblib

ROOT = Path(__file__).resolve().parent
RESEARCH = ROOT / "research"
for item in (ROOT, RESEARCH):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

import orderflow_classifier_walkforward as classifier  # noqa: E402
import orderflow_ml_walkforward as base  # noqa: E402


SYMBOLS = [
    "AAVEUSDT", "ADAUSDT", "APTUSDT", "BTCUSDT", "CRVUSDT",
    "DOGEUSDT", "ETHUSDT", "FILUSDT", "LINKUSDT", "NEARUSDT",
    "RENDERUSDT", "SOLUSDT", "TAOUSDT", "XLMUSDT", "XRPUSDT",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", default="data/market_cache_orderflow_15m_430d")
    parser.add_argument("--output", default="models/orderflow_classifier.joblib")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    histories = base.load_histories(ROOT / args.cache_dir, SYMBOLS)
    btc_by_time = {candle.open_time: candle for candle in histories["BTCUSDT"]}
    features = {}
    atr_values = {}
    for index, symbol in enumerate(SYMBOLS):
        features[symbol], atr_values[symbol] = base.build_base_features(
            histories[symbol], btc_by_time, index, len(SYMBOLS)
        )
    data_end = min(candles[-1].close_time + 1 for candles in histories.values())
    period = classifier.fit_period_model(
        histories, features, atr_values, len(SYMBOLS), data_end
    )
    if period is None:
        raise RuntimeError("not enough data to fit the order-flow model")
    eligible = [
        quantile
        for quantile, metrics in period.calibration.items()
        if metrics["average_r"] > 0
        and metrics["win_rate"] >= 45
        and metrics.get("profit_factor", 0.0) >= 1.1
    ]
    selected = min(eligible) if eligible else None
    trained_at = dt.datetime.fromtimestamp(data_end / 1000, dt.timezone.utc)
    artifact = {
        "version": 1,
        "enabled": selected is not None,
        "message": "ready" if selected is not None else "calibration gate rejected model",
        "model": period.estimator.model,
        "symbols": SYMBOLS,
        "quantile": selected,
        "threshold": period.thresholds[selected] if selected is not None else None,
        "calibration": period.calibration.get(selected, {}) if selected is not None else {},
        "all_calibrations": period.calibration,
        "trained_at": trained_at.isoformat(timespec="seconds"),
        "expires_at": (trained_at + dt.timedelta(days=35)).isoformat(timespec="seconds"),
        "trade_profile": {
            "source": "orderflow_ml",
            "entry_order_type": "market",
            "target_order_type": "limit",
            "stop_atr": 1.5,
            "target_atr": 3.0,
            "max_holding_bars": 16,
            "trail_after_r": 99.0,
            "trail_atr": 1.5,
        },
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, output, compress=3)
    print(f"saved {output}")
    print(f"enabled={artifact['enabled']} quantile={selected} threshold={artifact['threshold']}")
    print(f"calibration={artifact['calibration']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
