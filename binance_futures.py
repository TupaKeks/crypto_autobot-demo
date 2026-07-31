#!/usr/bin/env python3
"""Small Binance USD-M Futures adapter used by Crypto Autobot."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any


DEMO_BASE_URL = "https://demo-fapi.binance.com"
LIVE_BASE_URL = "https://fapi.binance.com"
LIVE_CONFIRMATION = "I_UNDERSTAND_REAL_MONEY"


class BinanceAPIError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        if isinstance(payload, dict):
            message = f"Binance error {payload.get('code')}: {payload.get('msg')}"
        else:
            message = f"Binance HTTP {status}: {payload}"
        super().__init__(message)

    @property
    def code(self) -> int | None:
        if isinstance(self.payload, dict) and self.payload.get("code") is not None:
            return int(self.payload["code"])
        return None


@dataclass(frozen=True)
class SymbolRules:
    quantity_step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    price_tick: Decimal
    min_notional: Decimal


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def price_to_tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    mode = ROUND_UP if rounding == "up" else ROUND_DOWN
    return (value / tick).to_integral_value(rounding=mode) * tick


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


class BinanceFuturesBroker:
    """Signed REST client plus the small set of operations the bot needs."""

    def __init__(
        self,
        *,
        environment: str,
        api_key: str,
        secret_key: str,
        recv_window_ms: int = 5000,
        orders_enabled: bool = False,
        live_confirmation: str = "",
        quote_asset: str = "USDT",
        leverage: int = 2,
        margin_type: str = "ISOLATED",
        working_type: str = "MARK_PRICE",
        price_protect: bool = False,
    ):
        if environment not in ("demo", "live"):
            raise ValueError("Binance environment must be demo or live.")
        if not api_key or not secret_key:
            env_prefix = "BINANCE_DEMO" if environment == "demo" else "BINANCE_LIVE"
            raise ValueError(f"Set {env_prefix}_API_KEY and {env_prefix}_API_SECRET.")
        if environment == "live" and orders_enabled and live_confirmation != LIVE_CONFIRMATION:
            raise ValueError(
                "Live order safety lock is active. Pass "
                f"--confirm-live {LIVE_CONFIRMATION} together with --enable-orders."
            )
        if leverage < 1 or leverage > 5:
            raise ValueError("Safety limit: leverage must be between 1 and 5.")
        if margin_type.upper() != "ISOLATED":
            raise ValueError("Safety limit: only ISOLATED margin is supported.")
        if working_type not in ("MARK_PRICE", "CONTRACT_PRICE"):
            raise ValueError("working_type must be MARK_PRICE or CONTRACT_PRICE.")

        self.environment = environment
        self.base_url = DEMO_BASE_URL if environment == "demo" else LIVE_BASE_URL
        self.api_key = api_key
        self.secret_key = secret_key.encode("utf-8")
        self.recv_window_ms = recv_window_ms
        self.orders_enabled = orders_enabled
        self.quote_asset = quote_asset
        self.leverage = leverage
        self.margin_type = margin_type.upper()
        self.working_type = working_type
        self.price_protect = price_protect
        self.time_offset_ms = 0
        self._rules: dict[str, SymbolRules] = {}

    def public(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(method, path, params or {}, signed=False)

    def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        signed_params = dict(params or {})
        signed_params["recvWindow"] = self.recv_window_ms
        signed_params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
        return self._request(method, path, signed_params, signed=True)

    def sync_time(self) -> int:
        server_time = int(self.public("GET", "/fapi/v1/time")["serverTime"])
        self.time_offset_ms = server_time - int(time.time() * 1000)
        return self.time_offset_ms

    def _request(self, method: str, path: str, params: dict[str, Any], signed: bool) -> Any:
        query = urllib.parse.urlencode(params)
        if signed:
            signature = hmac.new(self.secret_key, query.encode("utf-8"), hashlib.sha256).hexdigest()
            query = f"{query}&signature={signature}"
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {"User-Agent": "crypto-autobot/1.0"}
        if signed:
            headers["X-MBX-APIKEY"] = self.api_key
        request = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            raise BinanceAPIError(exc.code, payload) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Binance connection failed: {exc.reason}") from exc

    def require_orders_enabled(self) -> None:
        if not self.orders_enabled:
            raise PermissionError("Binance orders are disabled. Restart with --enable-orders.")

    def verify_one_way_mode(self) -> None:
        mode = self.signed("GET", "/fapi/v1/positionSide/dual")
        if bool(mode.get("dualSidePosition")):
            raise ValueError("Binance account must use One-Way Mode, not Hedge Mode.")

    def get_balance(self) -> dict[str, Any]:
        balances = self.signed("GET", "/fapi/v3/balance")
        for balance in balances:
            if balance.get("asset") == self.quote_asset:
                return balance
        raise ValueError(f"No {self.quote_asset} futures balance found.")

    def get_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        return list(self.signed("GET", "/fapi/v3/positionRisk", params))

    def get_open_positions(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.get_positions()
            if Decimal(str(item.get("positionAmt", "0"))) != 0
        ]

    def get_open_position(self, symbol: str) -> dict[str, Any] | None:
        for position in self.get_positions(symbol):
            if Decimal(str(position.get("positionAmt", "0"))) != 0:
                return position
        return None

    def account_summary(self) -> dict[str, Any]:
        self.sync_time()
        self.verify_one_way_mode()
        balance = self.get_balance()
        positions = self.get_open_positions()
        return {
            "environment": self.environment,
            "base_url": self.base_url,
            "asset": balance["asset"],
            "balance": balance["balance"],
            "available_balance": balance["availableBalance"],
            "positions": positions,
            "position_mode": "one-way",
            "orders_enabled": self.orders_enabled,
        }

    def symbol_rules(self, symbol: str) -> SymbolRules:
        if symbol in self._rules:
            return self._rules[symbol]
        info = self.public("GET", "/fapi/v1/exchangeInfo")
        symbol_info = next((item for item in info["symbols"] if item["symbol"] == symbol), None)
        if not symbol_info:
            raise ValueError(f"Symbol {symbol} is missing from Binance exchangeInfo.")
        filters = {item["filterType"]: item for item in symbol_info["filters"]}
        lot = filters.get("MARKET_LOT_SIZE") or filters["LOT_SIZE"]
        price_filter = filters["PRICE_FILTER"]
        notional = filters.get("MIN_NOTIONAL", {})
        rules = SymbolRules(
            quantity_step=Decimal(str(lot["stepSize"])),
            min_quantity=Decimal(str(lot["minQty"])),
            max_quantity=Decimal(str(lot["maxQty"])),
            price_tick=Decimal(str(price_filter["tickSize"])),
            min_notional=Decimal(str(notional.get("notional", "0"))),
        )
        self._rules[symbol] = rules
        return rules

    def set_symbol_risk(self, symbol: str) -> None:
        self.signed(
            "POST",
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": self.leverage},
        )
        try:
            self.signed(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": self.margin_type},
            )
        except BinanceAPIError as exc:
            if exc.code != -4046:
                raise

    def cancel_protection(self, symbol: str) -> None:
        try:
            self.signed("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})
        except BinanceAPIError as exc:
            if exc.code not in (-2011, -2013):
                raise

    def get_open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        return list(
            self.signed(
                "GET",
                "/fapi/v1/openAlgoOrders",
                {"symbol": symbol, "algoType": "CONDITIONAL"},
            )
        )

    def has_stop_and_target(self, symbol: str) -> bool:
        order_types = {
            str(order.get("orderType", "")).upper()
            for order in self.get_open_algo_orders(symbol)
            if str(order.get("algoStatus", "NEW")).upper() == "NEW"
        }
        return "STOP_MARKET" in order_types and "TAKE_PROFIT_MARKET" in order_types

    def market_close(self, symbol: str, position: dict[str, Any]) -> dict[str, Any]:
        self.require_orders_enabled()
        amount = Decimal(str(position["positionAmt"]))
        side = "SELL" if amount > 0 else "BUY"
        return self.signed(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": decimal_text(abs(amount)),
                "reduceOnly": "true",
                "newOrderRespType": "RESULT",
                "newClientOrderId": f"autobot-close-{uuid.uuid4().hex[:18]}",
            },
        )

    def place_protection(
        self,
        symbol: str,
        exit_side: str,
        stop: Decimal,
        target: Decimal,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.require_orders_enabled()
        common = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": exit_side,
            "closePosition": "true",
            "workingType": self.working_type,
            "priceProtect": str(self.price_protect).lower(),
        }
        stop_order = self.signed(
            "POST",
            "/fapi/v1/algoOrder",
            {
                **common,
                "type": "STOP_MARKET",
                "triggerPrice": decimal_text(stop),
                "clientAlgoId": f"autobot-sl-{uuid.uuid4().hex[:20]}",
            },
        )
        try:
            target_order = self.signed(
                "POST",
                "/fapi/v1/algoOrder",
                {
                    **common,
                    "type": "TAKE_PROFIT_MARKET",
                    "triggerPrice": decimal_text(target),
                    "clientAlgoId": f"autobot-tp-{uuid.uuid4().hex[:20]}",
                },
            )
        except Exception:
            self.cancel_protection(symbol)
            raise
        return stop_order, target_order

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
        self.require_orders_enabled()
        self.verify_one_way_mode()
        if self.get_open_position(symbol):
            raise ValueError(f"{symbol} already has an open Binance position.")
        if len(self.get_open_positions()) >= max_open_positions:
            raise ValueError("Maximum number of Binance positions reached.")
        if stop_distance <= 0 or target_distance <= 0:
            raise ValueError("Stop and target distances must be positive.")

        self.cancel_protection(symbol)
        self.set_symbol_risk(symbol)
        balance = self.get_balance()
        wallet_balance = Decimal(str(balance["balance"]))
        available_balance = Decimal(str(balance["availableBalance"]))
        if available_balance <= 0:
            raise ValueError(f"No available {self.quote_asset} balance.")

        mark = self.public("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})
        mark_price = Decimal(str(mark["markPrice"]))
        risk_cash = wallet_balance * Decimal(str(risk_percent)) / Decimal("100")
        quantity = risk_cash / Decimal(str(stop_distance))

        # A risk-based size can exceed available margin when the stop is tight.
        margin_cap_notional = available_balance * Decimal(str(self.leverage)) * Decimal("0.95")
        quantity = min(quantity, margin_cap_notional / mark_price)

        rules = self.symbol_rules(symbol)
        quantity = min(floor_to_step(quantity, rules.quantity_step), rules.max_quantity)
        if quantity < rules.min_quantity:
            raise ValueError(
                f"Calculated quantity {decimal_text(quantity)} is below Binance minimum "
                f"{decimal_text(rules.min_quantity)}."
            )
        if rules.min_notional and quantity * mark_price < rules.min_notional:
            raise ValueError(
                f"Order notional is below Binance minimum {decimal_text(rules.min_notional)} "
                f"{self.quote_asset}."
            )

        entry_side = "BUY" if side == "long" else "SELL"
        exit_side = "SELL" if side == "long" else "BUY"
        client_order_id = f"autobot-entry-{uuid.uuid4().hex[:18]}"
        try:
            entry = self.signed(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": symbol,
                    "side": entry_side,
                    "type": "MARKET",
                    "quantity": decimal_text(quantity),
                    "newOrderRespType": "RESULT",
                    "newClientOrderId": client_order_id,
                },
            )
        except BinanceAPIError as exc:
            if exc.status != 503:
                raise
            # A 503 can mean Binance accepted the order but the response was lost.
            entry = self.signed(
                "GET",
                "/fapi/v1/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
            )

        entry_price = Decimal(str(entry.get("avgPrice") or "0"))
        if entry_price <= 0:
            position = self.get_open_position(symbol)
            entry_price = Decimal(str(position["entryPrice"])) if position else mark_price

        stop_delta = Decimal(str(stop_distance))
        target_delta = Decimal(str(target_distance))
        if side == "long":
            stop = price_to_tick(entry_price - stop_delta, rules.price_tick, "down")
            target = price_to_tick(entry_price + target_delta, rules.price_tick, "down")
        else:
            stop = price_to_tick(entry_price + stop_delta, rules.price_tick, "up")
            target = price_to_tick(entry_price - target_delta, rules.price_tick, "up")
        if stop <= 0 or target <= 0:
            position = self.get_open_position(symbol)
            if position:
                self.market_close(symbol, position)
            raise ValueError("Calculated stop or target is invalid; emergency close sent.")

        try:
            stop_order, target_order = self.place_protection(
                symbol,
                exit_side,
                stop,
                target,
            )
        except Exception as exc:
            position = self.get_open_position(symbol)
            if position:
                self.market_close(symbol, position)
            raise RuntimeError(f"Protection failed; emergency close sent: {exc}") from exc

        return {
            "symbol": symbol,
            "side": side,
            "quantity": decimal_text(quantity),
            "entry": decimal_text(entry_price),
            "stop": decimal_text(stop),
            "target": decimal_text(target),
            "entry_order_id": entry.get("orderId"),
            "entry_client_order_id": client_order_id,
            "stop_algo_id": stop_order.get("algoId"),
            "target_algo_id": target_order.get("algoId"),
            "wallet_balance": str(wallet_balance),
            "opened_at_ms": int(time.time() * 1000),
        }

    def realized_pnl_since(self, symbol: str, start_time_ms: int) -> dict[str, float]:
        trades = self.signed(
            "GET",
            "/fapi/v1/userTrades",
            {
                "symbol": symbol,
                "startTime": max(0, int(start_time_ms) - 60_000),
                "limit": 1000,
            },
        )
        realized = Decimal("0")
        commission = Decimal("0")
        for trade in trades:
            if int(trade.get("time", 0)) < start_time_ms:
                continue
            realized += Decimal(str(trade.get("realizedPnl", "0")))
            if trade.get("commissionAsset") == self.quote_asset:
                commission += Decimal(str(trade.get("commission", "0")))
        return {
            "realized_pnl": float(realized),
            "commission": float(commission),
            "net_pnl": float(realized - commission),
        }
