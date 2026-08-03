"""MetaTrader 5 broker adapter with server-side SL/TP on every entry."""

from __future__ import annotations

import datetime as dt
import math
from typing import Any


LIVE_CONFIRMATION = "I_UNDERSTAND_REAL_MONEY"


def _asdict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    method = getattr(value, "_asdict", None)
    if callable(method):
        return dict(method())
    if isinstance(value, dict):
        return dict(value)
    return dict(vars(value))


def _floor_step(value: float, step: float, minimum: float, maximum: float) -> float:
    if step <= 0:
        raise ValueError("MT5 symbol volume_step must be positive.")
    rounded = math.floor((value + 1e-12) / step) * step
    return min(maximum, max(minimum, rounded))


class MT5Broker:
    """Thin exchange-neutral wrapper around the official MetaTrader5 package."""

    def __init__(
        self,
        *,
        environment: str,
        orders_enabled: bool,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        terminal_path: str | None = None,
        live_confirmation: str = "",
        symbol_map: dict[str, str] | None = None,
        magic: int = 260802,
        deviation_points: int = 20,
        mt5_module: Any | None = None,
    ):
        if environment not in ("demo", "live"):
            raise ValueError("MT5 environment must be demo or live.")
        if environment == "live" and orders_enabled and live_confirmation != LIVE_CONFIRMATION:
            raise ValueError("MT5 live order safety lock is active.")
        if mt5_module is None:
            try:
                import MetaTrader5 as mt5_module  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "Install the official MetaTrader5 Python package on the machine running the MT5 terminal."
                ) from exc
        self.mt5 = mt5_module
        self.environment = environment
        self.orders_enabled = bool(orders_enabled)
        self.symbol_map = symbol_map or {}
        self.magic = int(magic)
        self.deviation_points = int(deviation_points)

        kwargs: dict[str, Any] = {}
        if login is not None:
            kwargs["login"] = int(login)
        if password:
            kwargs["password"] = password
        if server:
            kwargs["server"] = server
        initialized = self.mt5.initialize(terminal_path, **kwargs) if terminal_path else self.mt5.initialize(**kwargs)
        if not initialized:
            raise RuntimeError(f"MT5 initialize failed: {self.mt5.last_error()}")
        account = self.mt5.account_info()
        if account is None:
            self.mt5.shutdown()
            raise RuntimeError(f"MT5 account_info failed after initialize: {self.mt5.last_error()}")
        trade_mode = getattr(account, "trade_mode", None)
        real_mode = getattr(self.mt5, "ACCOUNT_TRADE_MODE_REAL", None)
        if trade_mode is not None and real_mode is not None:
            connected_to_real = int(trade_mode) == int(real_mode)
            if environment == "demo" and connected_to_real:
                self.mt5.shutdown()
                raise ValueError("MT5 Demo profile is connected to a real-money account.")
            if environment == "live" and not connected_to_real:
                self.mt5.shutdown()
                raise ValueError("MT5 Live profile is not connected to a real-money account.")

    def shutdown(self) -> None:
        self.mt5.shutdown()

    def _broker_symbol(self, symbol: str) -> str:
        return self.symbol_map.get(symbol, symbol)

    def _internal_symbol(self, broker_symbol: str) -> str:
        for internal, mapped in self.symbol_map.items():
            if mapped == broker_symbol:
                return internal
        return broker_symbol

    def _symbol_info(self, symbol: str) -> tuple[str, Any]:
        broker_symbol = self._broker_symbol(symbol)
        info = self.mt5.symbol_info(broker_symbol)
        if info is None:
            raise ValueError(f"MT5 symbol not found: {broker_symbol}")
        if not bool(getattr(info, "visible", True)) and not self.mt5.symbol_select(broker_symbol, True):
            raise RuntimeError(f"MT5 could not enable symbol: {broker_symbol}")
        return broker_symbol, info

    def _timeframe(self, interval: str) -> tuple[int, int]:
        names = {
            "1m": ("TIMEFRAME_M1", 60),
            "5m": ("TIMEFRAME_M5", 5 * 60),
            "15m": ("TIMEFRAME_M15", 15 * 60),
            "30m": ("TIMEFRAME_M30", 30 * 60),
            "1h": ("TIMEFRAME_H1", 60 * 60),
            "4h": ("TIMEFRAME_H4", 4 * 60 * 60),
            "1d": ("TIMEFRAME_D1", 24 * 60 * 60),
        }
        if interval not in names:
            raise ValueError(f"Unsupported MT5 candle interval: {interval}")
        constant_name, seconds = names[interval]
        timeframe = getattr(self.mt5, constant_name, None)
        if timeframe is None:
            raise RuntimeError(f"MT5 module does not provide {constant_name}.")
        return int(timeframe), seconds

    @staticmethod
    def _rate_value(rate: Any, field: str, index: int, default: Any = 0) -> Any:
        if isinstance(rate, dict):
            return rate.get(field, default)
        try:
            return rate[field]
        except (IndexError, KeyError, TypeError, ValueError):
            return getattr(rate, field, rate[index] if isinstance(rate, (list, tuple)) else default)

    def fetch_candles(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        """Return closed broker-native bars in oldest-to-newest order."""
        if int(limit) <= 0:
            return []
        broker_symbol, _ = self._symbol_info(symbol)
        timeframe, interval_seconds = self._timeframe(interval)
        rates = self.mt5.copy_rates_from_pos(broker_symbol, timeframe, 1, int(limit))
        if rates is None:
            raise RuntimeError(
                f"MT5 candle history unavailable for {broker_symbol}: {self.mt5.last_error()}"
            )

        candles: list[dict[str, Any]] = []
        for rate in rates:
            open_time = int(self._rate_value(rate, "time", 0)) * 1000
            real_volume = float(self._rate_value(rate, "real_volume", 7, 0.0))
            tick_volume = float(self._rate_value(rate, "tick_volume", 5, 0.0))
            candles.append(
                {
                    "open_time": open_time,
                    "open": float(self._rate_value(rate, "open", 1)),
                    "high": float(self._rate_value(rate, "high", 2)),
                    "low": float(self._rate_value(rate, "low", 3)),
                    "close": float(self._rate_value(rate, "close", 4)),
                    "volume": real_volume if real_volume > 0 else tick_volume,
                    "close_time": open_time + interval_seconds * 1000 - 1,
                }
            )
        candles.sort(key=lambda item: int(item["open_time"]))
        return candles

    def _accepted_retcodes(self) -> set[int]:
        names = ("TRADE_RETCODE_DONE", "TRADE_RETCODE_PLACED", "TRADE_RETCODE_DONE_PARTIAL")
        return {int(getattr(self.mt5, name)) for name in names if hasattr(self.mt5, name)}

    def _send(self, request: dict[str, Any]) -> Any:
        if not self.orders_enabled:
            raise ValueError("MT5 orders are disabled for this process.")
        result = self.mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"MT5 order_send returned no result: {self.mt5.last_error()}")
        if int(getattr(result, "retcode", -1)) not in self._accepted_retcodes():
            raise RuntimeError(
                f"MT5 order failed: retcode={getattr(result, 'retcode', None)} "
                f"comment={getattr(result, 'comment', '')}"
            )
        return result

    def _volume(
        self,
        symbol: str,
        side: str,
        entry: float,
        stop: float,
        risk_percent: float,
    ) -> float:
        broker_symbol, info = self._symbol_info(symbol)
        account = self.mt5.account_info()
        if account is None:
            raise RuntimeError(f"MT5 account_info failed: {self.mt5.last_error()}")
        balance = float(getattr(account, "balance"))
        order_type = self.mt5.ORDER_TYPE_BUY if side == "long" else self.mt5.ORDER_TYPE_SELL
        one_lot_loss = self.mt5.order_calc_profit(order_type, broker_symbol, 1.0, entry, stop)
        if one_lot_loss is None or abs(float(one_lot_loss)) <= 0:
            raise RuntimeError("MT5 could not calculate stop-loss value for one lot.")
        raw = balance * float(risk_percent) / 100 / abs(float(one_lot_loss))
        if raw < float(getattr(info, "volume_min")):
            raise ValueError(
                f"Calculated MT5 volume {raw:.8f} is below the broker minimum "
                f"{float(getattr(info, 'volume_min')):.8f}; refusing to exceed configured risk."
            )
        return _floor_step(
            raw,
            float(getattr(info, "volume_step")),
            float(getattr(info, "volume_min")),
            float(getattr(info, "volume_max")),
        )

    def _normalize_position(self, position: Any) -> dict[str, Any]:
        item = _asdict(position)
        side = "long" if int(item.get("type", 0)) == int(self.mt5.POSITION_TYPE_BUY) else "short"
        return {
            "symbol": self._internal_symbol(str(item.get("symbol", ""))),
            "side": side,
            "quantity": float(item.get("volume", 0.0)),
            "entry": float(item.get("price_open", 0.0)),
            "stop": float(item.get("sl", 0.0)),
            "target": float(item.get("tp", 0.0)),
            "unrealized_pnl": float(item.get("profit", 0.0)),
            "position_ticket": int(item.get("ticket", 0)),
            "opened_at_ms": int(item.get("time_msc", int(item.get("time", 0)) * 1000)),
            "managed": int(item.get("magic", 0)) == self.magic,
        }

    def account_summary(self) -> dict[str, Any]:
        account = self.mt5.account_info()
        if account is None:
            raise RuntimeError(f"MT5 account_info failed: {self.mt5.last_error()}")
        item = _asdict(account)
        return {
            "environment": self.environment,
            "asset": str(item.get("currency", "")),
            "balance": float(item.get("balance", 0.0)),
            "available_balance": float(item.get("margin_free", 0.0)),
            "positions": self.get_open_positions(),
            "position_mode": "hedging" if bool(item.get("margin_mode") == 2) else "netting",
            "orders_enabled": self.orders_enabled,
        }

    def get_balance(self) -> dict[str, Any]:
        summary = self.account_summary()
        return {
            "balance": summary["balance"],
            "availableBalance": summary["available_balance"],
        }

    def get_open_positions(self) -> list[dict[str, Any]]:
        positions = self.mt5.positions_get() or ()
        return [self._normalize_position(position) for position in positions]

    def get_open_position(self, symbol: str) -> dict[str, Any] | None:
        broker_symbol = self._broker_symbol(symbol)
        positions = self.mt5.positions_get(symbol=broker_symbol) or ()
        managed = [item for item in positions if int(getattr(item, "magic", self.magic)) == self.magic]
        return self._normalize_position(managed[0]) if managed else None

    def _entry_request(
        self,
        *,
        symbol: str,
        side: str,
        entry: float,
        stop_distance: float,
        target_distance: float,
        risk_percent: float,
        pending: bool,
    ) -> dict[str, Any]:
        broker_symbol, info = self._symbol_info(symbol)
        digits = int(getattr(info, "digits", 8))
        stop = entry - stop_distance if side == "long" else entry + stop_distance
        target = entry + target_distance if side == "long" else entry - target_distance
        order_type = (
            self.mt5.ORDER_TYPE_BUY_LIMIT if pending and side == "long"
            else self.mt5.ORDER_TYPE_SELL_LIMIT if pending
            else self.mt5.ORDER_TYPE_BUY if side == "long"
            else self.mt5.ORDER_TYPE_SELL
        )
        return {
            "action": self.mt5.TRADE_ACTION_PENDING if pending else self.mt5.TRADE_ACTION_DEAL,
            "symbol": broker_symbol,
            "volume": self._volume(symbol, side, entry, stop, risk_percent),
            "type": order_type,
            "price": round(entry, digits),
            "sl": round(stop, digits),
            "tp": round(target, digits),
            "deviation": self.deviation_points,
            "magic": self.magic,
            "comment": "crypto-autobot",
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": (
                self.mt5.ORDER_FILLING_RETURN
                if pending
                else getattr(info, "filling_mode", self.mt5.ORDER_FILLING_RETURN)
            ),
        }

    def open_position(
        self,
        *,
        symbol: str,
        side: str,
        stop_distance: float,
        target_distance: float,
        risk_percent: float,
        max_open_positions: int,
    ) -> dict[str, Any]:
        if len(self.get_open_positions()) >= int(max_open_positions):
            raise ValueError("MT5 maximum open positions reached.")
        if self.get_open_position(symbol):
            raise ValueError(f"MT5 position already exists for {symbol}.")
        broker_symbol, _ = self._symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(broker_symbol)
        if tick is None:
            raise RuntimeError(f"MT5 tick unavailable for {broker_symbol}.")
        entry = float(tick.ask if side == "long" else tick.bid)
        request = self._entry_request(
            symbol=symbol,
            side=side,
            entry=entry,
            stop_distance=stop_distance,
            target_distance=target_distance,
            risk_percent=risk_percent,
            pending=False,
        )
        result = self._send(request)
        return {
            "symbol": symbol,
            "side": side,
            "quantity": request["volume"],
            "entry": float(getattr(result, "price", 0.0) or entry),
            "stop": request["sl"],
            "target": request["tp"],
            "entry_order_id": int(getattr(result, "order", 0)),
            "opened_at_ms": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
            "wallet_balance": float(self.get_balance()["balance"]),
        }

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
    ) -> dict[str, Any]:
        if len(self.get_open_positions()) >= int(max_open_positions):
            raise ValueError("MT5 maximum open positions reached.")
        request = self._entry_request(
            symbol=symbol,
            side=side,
            entry=float(limit_price),
            stop_distance=stop_distance,
            target_distance=target_distance,
            risk_percent=risk_percent,
            pending=True,
        )
        result = self._send(request)
        ticket = int(getattr(result, "order", 0))
        return {
            "symbol": symbol,
            "side": side,
            "quantity": request["volume"],
            "limit_price": request["price"],
            "stop": request["sl"],
            "target": request["tp"],
            "entry_order_id": ticket,
            "entry_client_order_id": str(ticket),
            "order_status": "NEW",
            "wallet_balance": float(self.get_balance()["balance"]),
        }

    def get_entry_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        ticket = int(client_order_id)
        active = self.mt5.orders_get(ticket=ticket) or ()
        if active:
            return {"symbol": symbol, "clientOrderId": client_order_id, "status": "NEW"}
        if self.get_open_position(symbol):
            return {"symbol": symbol, "clientOrderId": client_order_id, "status": "FILLED"}
        return {"symbol": symbol, "clientOrderId": client_order_id, "status": "CANCELED"}

    def cancel_entry_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        self._send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": int(client_order_id)})
        return {"symbol": symbol, "clientOrderId": client_order_id, "status": "CANCELED"}

    def activate_limit_entry(
        self,
        *,
        symbol: str,
        side: str,
        client_order_id: str,
        stop_distance: float,
        target_distance: float,
    ) -> dict[str, Any]:
        position = self.get_open_position(symbol)
        if position is None:
            return {"status": "NEW", "symbol": symbol, "side": side}
        if not position["stop"] or not position["target"]:
            raise RuntimeError(f"MT5 filled {symbol} without attached SL/TP.")
        return {
            "status": "FILLED",
            **position,
            "entry_order_id": int(client_order_id),
            "wallet_balance": float(self.get_balance()["balance"]),
        }

    def has_stop_and_target(self, symbol: str) -> bool:
        position = self.get_open_position(symbol)
        return bool(position and position["stop"] and position["target"])

    def cancel_protection(self, symbol: str) -> None:
        # MT5 SL/TP are attached to the position. Keeping them active until the
        # close deal is accepted avoids a temporary unprotected position.
        return

    def market_close(self, symbol: str, position: dict[str, Any]) -> dict[str, Any]:
        broker_symbol, info = self._symbol_info(symbol)
        tick = self.mt5.symbol_info_tick(broker_symbol)
        if tick is None:
            raise RuntimeError(f"MT5 tick unavailable for {broker_symbol}.")
        close_side = "short" if position["side"] == "long" else "long"
        price = float(tick.bid if position["side"] == "long" else tick.ask)
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": broker_symbol,
            "volume": float(position["quantity"]),
            "type": self.mt5.ORDER_TYPE_SELL if close_side == "short" else self.mt5.ORDER_TYPE_BUY,
            "position": int(position["position_ticket"]),
            "price": price,
            "deviation": self.deviation_points,
            "magic": self.magic,
            "comment": "crypto-autobot-close",
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": getattr(info, "filling_mode", self.mt5.ORDER_FILLING_RETURN),
        }
        result = self._send(request)
        return {"symbol": symbol, "price": float(getattr(result, "price", 0.0) or price)}

    def realized_pnl_since(self, symbol: str, start_time_ms: int) -> dict[str, float]:
        start = dt.datetime.fromtimestamp(start_time_ms / 1000, dt.timezone.utc)
        end = dt.datetime.now(dt.timezone.utc)
        deals = self.mt5.history_deals_get(start, end, group=self._broker_symbol(symbol)) or ()
        managed = [deal for deal in deals if int(getattr(deal, "magic", self.magic)) == self.magic]
        profit = sum(float(getattr(deal, "profit", 0.0)) for deal in managed)
        costs = sum(
            float(getattr(deal, "commission", 0.0))
            + float(getattr(deal, "swap", 0.0))
            + float(getattr(deal, "fee", 0.0))
            for deal in managed
        )
        return {
            "realized_pnl": profit,
            "commission": -costs,
            "net_pnl": profit + costs,
        }
