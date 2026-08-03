"""Optional cost-aware order-flow classifier used by the live bot."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from typing import Any

try:
    from .orderflow_features import MINIMUM_HISTORY, build_base_features, directional_features
except ImportError:
    from orderflow_features import MINIMUM_HISTORY, build_base_features, directional_features


@dataclasses.dataclass(frozen=True)
class OrderflowDecision:
    side: str | None
    score: float | None
    threshold: float | None
    atr: float | None
    status: str


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _load_artifact(path: Path) -> dict[str, Any]:
    try:
        import joblib
    except ImportError as exc:
        raise RuntimeError("ML dependency missing; install requirements-ml.txt") from exc
    resolved = str(path.resolve())
    modified = path.stat().st_mtime
    cached = _CACHE.get(resolved)
    if cached and cached[0] == modified:
        return cached[1]
    artifact = joblib.load(path)
    if not isinstance(artifact, dict) or "model" not in artifact:
        raise ValueError("invalid order-flow model artifact")
    _CACHE[resolved] = (modified, artifact)
    return artifact


def model_status(config: dict[str, Any], root: Path) -> dict[str, Any]:
    if not config.get("enabled", False):
        return {"enabled": False, "ready": False, "message": "disabled"}
    path = root / str(config.get("model_path", "models/orderflow_classifier.joblib"))
    if not path.exists():
        return {"enabled": True, "ready": False, "message": "model file missing", "path": str(path)}
    try:
        artifact = _load_artifact(path)
        expires_at = str(artifact.get("expires_at", ""))
        if expires_at:
            expires = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if dt.datetime.now(dt.timezone.utc) > expires:
                return {"enabled": True, "ready": False, "message": "model expired", "path": str(path)}
        return {
            "enabled": True,
            "ready": bool(artifact.get("enabled", True)),
            "message": str(artifact.get("message", "ready")),
            "path": str(path),
            "trained_at": artifact.get("trained_at"),
            "expires_at": artifact.get("expires_at"),
            "threshold": artifact.get("threshold"),
            "quantile": artifact.get("quantile"),
            "calibration": artifact.get("calibration", {}),
        }
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "ready": False, "message": str(exc), "path": str(path)}


def evaluate_orderflow_signal(
    config: dict[str, Any],
    root: Path,
    symbol: str,
    candles: list[Any],
    btc_candles: list[Any],
) -> OrderflowDecision:
    status = model_status(config, root)
    if not status["ready"]:
        return OrderflowDecision(None, None, status.get("threshold"), None, status["message"])
    artifact = _load_artifact(Path(status["path"]))
    symbols = [str(item).upper() for item in artifact.get("symbols", [])]
    if symbol not in symbols:
        return OrderflowDecision(None, None, float(artifact["threshold"]), None, "symbol outside ML universe")
    if len(candles) < MINIMUM_HISTORY or len(btc_candles) < MINIMUM_HISTORY:
        return OrderflowDecision(None, None, float(artifact["threshold"]), None, "ML history unavailable")
    rows, atr_values = build_base_features(
        candles,
        {candle.open_time: candle for candle in btc_candles},
        symbols.index(symbol),
        len(symbols),
    )
    row = rows[-1]
    atr_value = atr_values[-1]
    if row is None or atr_value is None:
        return OrderflowDecision(None, None, float(artifact["threshold"]), None, "ML features unavailable")
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("ML dependency missing; install requirements-ml.txt") from exc
    candidates = []
    for direction, side in ((1, "long"), (-1, "short")):
        features = directional_features(row, direction, len(symbols))
        score = float(artifact["model"].predict_proba(np.asarray([features], dtype=np.float32))[0, 1])
        candidates.append((score, side))
    score, side = max(candidates)
    threshold = float(artifact["threshold"])
    if score < threshold:
        return OrderflowDecision(None, score, threshold, float(atr_value), f"ML score {score:.3f} < {threshold:.3f}")
    return OrderflowDecision(side, score, threshold, float(atr_value), f"ML {side} {score:.3f}")
