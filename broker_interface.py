"""Exchange-neutral contracts used by the strategy/runtime boundary."""

from __future__ import annotations

from typing import Any, Protocol


class BrokerAdapter(Protocol):
    """Minimal contract implemented by Binance and future MT5 adapters."""

    def account_summary(self) -> dict[str, Any]: ...

    def fetch_candles(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]: ...

    def open_position(
        self,
        *,
        symbol: str,
        side: str,
        stop_distance: float,
        target_distance: float,
        risk_percent: float,
        max_open_positions: int,
    ) -> dict[str, Any]: ...

    def place_limit_entry(
        self,
        *,
        symbol: str,
        side: str,
        limit_price: float,
        stop_distance: float,
        target_distance: float,
        risk_percent: float,
        max_open_positions: int,
    ) -> dict[str, Any]: ...

    def get_entry_order(self, symbol: str, client_order_id: str) -> dict[str, Any]: ...

    def cancel_entry_order(self, symbol: str, client_order_id: str) -> dict[str, Any]: ...

    def activate_limit_entry(
        self,
        *,
        symbol: str,
        side: str,
        client_order_id: str,
        stop_distance: float,
        target_distance: float,
    ) -> dict[str, Any]: ...

    def get_open_position(self, symbol: str) -> dict[str, Any] | None: ...

    def get_open_positions(self) -> list[dict[str, Any]]: ...

    def has_stop_and_target(self, symbol: str) -> bool: ...

    def market_close(self, symbol: str, position: dict[str, Any]) -> dict[str, Any]: ...

    def cancel_protection(self, symbol: str) -> None: ...

    def realized_pnl_since(self, symbol: str, start_time_ms: int) -> dict[str, float]: ...

    def get_balance(self) -> dict[str, Any]: ...
