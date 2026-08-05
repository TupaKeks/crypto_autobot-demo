from __future__ import annotations

import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from crypto_autobot.binance_futures import (
    BinanceAPIError,
    BinanceFuturesBroker,
    LIVE_CONFIRMATION,
    SymbolRules,
    floor_to_step,
    price_to_tick,
)
from crypto_autobot.bot import (
    BotContext,
    Candle,
    DEMO_TEST_CONFIRMATION,
    RuntimeController,
    adx,
    append_trade,
    broker_watchdog_once,
    broker_readiness_snapshot,
    build_context,
    close_position,
    ensure_state,
    ensure_trades_file,
    fetch_klines,
    fetch_market_candles,
    health_snapshot,
    manage_position,
    market_data_is_fresh,
    normalize_broker_position,
    open_position,
    open_demo_test_order,
    place_pending_entry,
    public_state,
    record_scan_diagnostic,
    reconcile_pending_entry,
    scan_once,
    write_state,
    load_config,
)


class FakeBroker:
    order_status = "FILLED"

    def __init__(self):
        self.cancelled_protection = []
        self.market_closes = []
        self.last_open_kwargs = None
        self.last_limit_kwargs = None
        self.activation_calls = []
        self.exchange_positions = []
        self.protection_ok = True

    def open_position(self, **kwargs):
        self.last_open_kwargs = kwargs
        return {
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "quantity": "0.25",
            "entry": "100",
            "stop": "96",
            "target": "106",
            "entry_order_id": 1,
            "stop_algo_id": 2,
            "target_algo_id": 3,
            "wallet_balance": "1000",
            "opened_at_ms": 123456789,
        }

    def place_limit_entry(self, **kwargs):
        self.last_limit_kwargs = kwargs
        return {
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "quantity": "0.25",
            "limit_price": str(kwargs["limit_price"]),
            "entry_order_id": 10,
            "entry_client_order_id": "autobot-limit-test",
            "order_status": "NEW",
            "wallet_balance": "1000",
        }

    def get_entry_order(self, symbol, client_order_id):
        return {"symbol": symbol, "clientOrderId": client_order_id, "status": self.order_status}

    def cancel_entry_order(self, symbol, client_order_id):
        self.order_status = "CANCELED"
        return self.get_entry_order(symbol, client_order_id)

    def activate_limit_entry(self, **kwargs):
        self.activation_calls.append(kwargs)
        return {
            "status": "FILLED",
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "quantity": "0.25",
            "entry": "99.8",
            "stop": "95.8",
            "target": "105.8",
            "entry_order_id": 10,
            "stop_algo_id": 11,
            "target_algo_id": 12,
            "wallet_balance": "1000",
            "opened_at_ms": 123456789,
        }

    def has_stop_and_target(self, symbol):
        return self.protection_ok

    def account_summary(self):
        return {
            "balance": 1000,
            "available_balance": 1000,
            "positions": list(self.exchange_positions),
            "environment": "demo",
            "position_mode": "ONE_WAY",
        }

    def get_open_positions(self):
        return list(self.exchange_positions)

    def get_open_position(self, symbol):
        return next((item for item in self.exchange_positions if item.get("symbol") == symbol), None)

    def get_balance(self):
        return {"balance": 1000, "availableBalance": 1000}

    def realized_pnl_since(self, symbol, start_time_ms):
        return {"realized_pnl": 0, "commission": 0, "net_pnl": 0}

    def cancel_protection(self, symbol):
        self.cancelled_protection.append(symbol)

    def market_close(self, symbol, position):
        self.market_closes.append((symbol, position))
        return {"status": "FILLED"}


def test_config(data_dir: str) -> dict:
    return {
        "app": {"timezone": "UTC", "data_dir": data_dir},
        "strategy": {
            "stop_atr": 2,
            "target_atr": 3,
            "trail_after_r": 1.2,
            "trail_atr": 1.5,
        },
        "account": {
            "initial_balance": 1000,
            "risk_per_trade_percent": 0.5,
            "max_open_positions": 2,
            "max_daily_trades": 4,
            "max_daily_loss_percent": 2,
        },
        "notifications": {"telegram_enabled": False},
    }


def mode_config(data_dir: str, mode: str) -> dict:
    config = test_config(data_dir)
    config["app"].update(
        {
            "name": "Test Autobot",
            "host": "127.0.0.1",
            "port": 8090,
            "scan_interval_seconds": 60,
        }
    )
    config["market"] = {
        "base_url": "https://fapi.binance.com",
        "symbols": ["BTCUSDT"],
        "interval": "4h",
        "history_limit": 320,
    }
    config["broker"] = {
        "mode": mode,
        "leverage": 2 if mode != "live" else 1,
        "margin_type": "ISOLATED",
    }
    config["strategy"]["enabled"] = mode != "live"
    return config


def write_mode_configs(root: Path, data_dir: str) -> dict[str, Path]:
    paths = {
        "paper": root / "config.example.json",
        "demo": root / "config.demo.example.json",
        "live": root / "config.live.example.json",
    }
    for mode, path in paths.items():
        path.write_text(json.dumps(mode_config(data_dir, mode)), encoding="utf-8")
    return paths


def seed_passing_demo_validation(data_dir: str) -> None:
    created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)
    pnl_values = [3.2, -2.0] * 60
    trades = []
    for pnl in pnl_values:
        trades.append({"event": "open", "pnl": 0})
        trades.append({"event": "close", "pnl": pnl})
    state = {
        "created_at": created_at.isoformat(),
        "initial_balance": 1000,
        "balance": 1000 + sum(pnl_values),
        "realized_pnl": sum(pnl_values),
        "trades": trades,
    }
    (Path(data_dir) / "state_demo.json").write_text(json.dumps(state), encoding="utf-8")


class BinanceMathTests(unittest.TestCase):
    def test_broker_positions_are_normalized_for_the_runtime(self):
        binance = normalize_broker_position({
            "symbol": "BTCUSDT",
            "positionAmt": "-0.5",
            "entryPrice": "100",
            "unRealizedProfit": "3.2",
        })
        mt5 = normalize_broker_position({
            "symbol": "BTCUSDT",
            "side": "long",
            "quantity": 0.25,
            "entry": 101.0,
            "stop": 98.0,
            "target": 106.0,
            "unrealized_pnl": 1.2,
            "position_ticket": 7,
        })

        self.assertEqual((binance["side"], binance["quantity"]), ("short", 0.5))
        self.assertEqual(binance["unrealized_pnl"], 3.2)
        self.assertEqual(mt5["position_ticket"], 7)
        self.assertEqual(mt5["target"], 106.0)


class BinanceBrokerTests(unittest.TestCase):
    def test_time_sync_uses_round_trip_midpoint(self):
        broker = BinanceFuturesBroker(
            environment="demo",
            api_key="key",
            secret_key="secret",
        )
        broker.public = lambda *_args, **_kwargs: {"serverTime": 1_100}

        with patch("crypto_autobot.binance_futures.time.time", side_effect=[1.0, 1.2]):
            offset = broker.sync_time()

        self.assertEqual(offset, 0)
        self.assertEqual(broker.last_time_sync_rtt_ms, 200)

    def test_signed_request_resyncs_and_retries_timestamp_error_once(self):
        broker = BinanceFuturesBroker(
            environment="demo",
            api_key="key",
            secret_key="secret",
        )
        calls = []

        def request(method, path, params, signed):
            calls.append((method, path, params, signed))
            if len(calls) == 1:
                raise BinanceAPIError(400, {"code": -1021, "msg": "timestamp outside recvWindow"})
            return {"status": "ok"}

        broker._request = request
        broker.sync_time = lambda: setattr(broker, "time_offset_ms", 250) or 250
        with patch("crypto_autobot.binance_futures.time.time", return_value=1.0):
            result = broker.signed("GET", "/fapi/v3/balance")

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2]["timestamp"], 1_000)
        self.assertEqual(calls[1][2]["timestamp"], 1_250)

    def test_signed_request_does_not_retry_other_binance_errors(self):
        broker = BinanceFuturesBroker(
            environment="demo",
            api_key="key",
            secret_key="secret",
        )
        broker._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BinanceAPIError(400, {"code": -2019, "msg": "margin is insufficient"})
        )
        broker.sync_time = lambda: self.fail("non-timestamp errors must not resync")

        with self.assertRaises(BinanceAPIError):
            broker.signed("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"})

    def test_limit_entry_is_post_only_and_tick_rounded(self):
        broker = BinanceFuturesBroker(
            environment="demo",
            api_key="key",
            secret_key="secret",
            orders_enabled=True,
        )
        captured = {}
        broker.verify_one_way_mode = lambda: None
        broker.get_open_position = lambda symbol: None
        broker.get_open_positions = lambda: []
        broker.cancel_protection = lambda symbol: None
        broker.set_symbol_risk = lambda symbol: None
        broker.get_balance = lambda: {"balance": "1000", "availableBalance": "1000"}
        broker.symbol_rules = lambda symbol: SymbolRules(
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            max_quantity=Decimal("100"),
            price_tick=Decimal("0.1"),
            min_notional=Decimal("5"),
        )

        def signed(method, path, params=None):
            captured.update({"method": method, "path": path, "params": params})
            return {"orderId": 123, "status": "NEW"}

        broker.signed = signed
        result = broker.place_limit_entry(
            symbol="BTCUSDT",
            side="long",
            limit_price=99.87,
            stop_distance=4,
            target_distance=6,
            risk_percent=0.5,
            max_open_positions=2,
        )
        self.assertEqual(captured["params"]["type"], "LIMIT")
        self.assertEqual(captured["params"]["timeInForce"], "GTX")
        self.assertEqual(captured["params"]["price"], "99.8")
        self.assertEqual(result["entry_order_id"], 123)

    def test_quantity_and_price_rounding(self):
        self.assertEqual(floor_to_step(Decimal("1.239"), Decimal("0.01")), Decimal("1.23"))
        self.assertEqual(price_to_tick(Decimal("100.19"), Decimal("0.1"), "down"), Decimal("100.1"))
        self.assertEqual(price_to_tick(Decimal("100.11"), Decimal("0.1"), "up"), Decimal("100.2"))

    def test_live_orders_require_exact_confirmation(self):
        with self.assertRaisesRegex(ValueError, "Live order safety lock"):
            BinanceFuturesBroker(
                environment="live",
                api_key="key",
                secret_key="secret",
                orders_enabled=True,
                live_confirmation="wrong",
            )
        broker = BinanceFuturesBroker(
            environment="live",
            api_key="key",
            secret_key="secret",
            orders_enabled=True,
            live_confirmation=LIVE_CONFIRMATION,
        )
        self.assertTrue(broker.orders_enabled)

    def test_leverage_safety_limit(self):
        with self.assertRaisesRegex(ValueError, "leverage"):
            BinanceFuturesBroker(
                environment="demo",
                api_key="key",
                secret_key="secret",
                leverage=10,
            )

    def test_exchange_protection_requires_stop_and_target(self):
        broker = BinanceFuturesBroker(
            environment="demo",
            api_key="key",
            secret_key="secret",
        )
        broker.signed = lambda method, path, params=None: [
            {
                "orderType": "STOP_MARKET",
                "algoStatus": "NEW",
                "clientAlgoId": "autobot-sl-test",
            },
            {
                "orderType": "TAKE_PROFIT_MARKET",
                "algoStatus": "NEW",
                "clientAlgoId": "autobot-tp-test",
            },
        ]
        self.assertTrue(broker.has_stop_and_target("BTCUSDT"))
        broker.signed = lambda method, path, params=None: [
            {
                "orderType": "STOP_MARKET",
                "algoStatus": "NEW",
                "clientAlgoId": "autobot-sl-test",
            }
        ]
        self.assertFalse(broker.has_stop_and_target("BTCUSDT"))

    def test_limit_target_is_post_only_reduce_only_protection(self):
        broker = BinanceFuturesBroker(
            environment="demo",
            api_key="key",
            secret_key="secret",
            orders_enabled=True,
            target_order_type="limit",
        )
        calls = []

        def signed(method, path, params=None):
            calls.append((method, path, params or {}))
            if path == "/fapi/v1/algoOrder":
                return {"algoId": 10}
            return {"orderId": 20, "status": "NEW"}

        broker.signed = signed
        stop, target = broker.place_protection(
            "BTCUSDT",
            "SELL",
            Decimal("95.1"),
            Decimal("110.2"),
            Decimal("0.25"),
        )

        self.assertEqual(stop["algoId"], 10)
        self.assertEqual(target["orderId"], 20)
        target_call = calls[1]
        self.assertEqual(target_call[:2], ("POST", "/fapi/v1/order"))
        self.assertEqual(target_call[2]["type"], "LIMIT")
        self.assertEqual(target_call[2]["timeInForce"], "GTX")
        self.assertEqual(target_call[2]["reduceOnly"], "true")
        self.assertEqual(target_call[2]["quantity"], "0.25")
        self.assertEqual(target_call[2]["price"], "110.2")

    def test_limit_target_and_market_stop_are_detected_together(self):
        broker = BinanceFuturesBroker(
            environment="demo",
            api_key="key",
            secret_key="secret",
            target_order_type="limit",
        )

        def signed(method, path, params=None):
            if path == "/fapi/v1/openAlgoOrders":
                return [{
                    "orderType": "STOP_MARKET",
                    "algoStatus": "NEW",
                    "clientAlgoId": "autobot-sl-test",
                }]
            if path == "/fapi/v1/openOrders":
                return [{
                    "type": "LIMIT",
                    "status": "PARTIALLY_FILLED",
                    "reduceOnly": True,
                    "clientOrderId": "autobot-tp-limit-test",
                }]
            raise AssertionError(path)

        broker.signed = signed
        self.assertTrue(broker.has_stop_and_target("BTCUSDT"))

    def test_cancel_protection_leaves_manual_orders_untouched(self):
        broker = BinanceFuturesBroker(
            environment="demo",
            api_key="key",
            secret_key="secret",
            target_order_type="limit",
        )
        deleted = []

        def signed(method, path, params=None):
            if method == "GET" and path == "/fapi/v1/openAlgoOrders":
                return [
                    {"algoId": 1, "clientAlgoId": "autobot-sl-test"},
                    {"algoId": 2, "clientAlgoId": "manual-stop"},
                ]
            if method == "GET" and path == "/fapi/v1/openOrders":
                return [
                    {"orderId": 3, "clientOrderId": "autobot-tp-limit-test"},
                    {"orderId": 4, "clientOrderId": "manual-target"},
                ]
            if method == "DELETE":
                deleted.append((path, params))
                return {}
            raise AssertionError((method, path))

        broker.signed = signed
        broker.cancel_protection("BTCUSDT")

        self.assertEqual(
            deleted,
            [
                ("/fapi/v1/algoOrder", {"algoId": 1}),
                ("/fapi/v1/order", {"symbol": "BTCUSDT", "orderId": 3}),
            ],
        )


class HealthSnapshotTests(unittest.TestCase):
    def make_context(self, tmp: str, mode: str = "paper") -> BotContext:
        config = mode_config(tmp, mode)
        config["app"]["health_stale_after_seconds"] = 180
        return BotContext(
            config=config,
            state_path=Path(tmp) / f"state_{mode}.json",
            trades_path=Path(tmp) / f"trades_{mode}.csv",
            timezone=ZoneInfo("UTC"),
            mode=mode,
            broker=None,
            orders_enabled=mode == "paper",
            exchange_snapshot={},
            lock=threading.Lock(),
            stop_event=threading.Event(),
        )

    def test_fresh_paper_scan_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make_context(tmp)
            state = ensure_state(ctx)
            state["runtime"]["last_scan_completed_at"] = "2026-08-03T10:00:00+00:00"

            payload, status = health_snapshot(
                ctx,
                state,
                now=dt.datetime(2026, 8, 3, 10, 1, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["heartbeat_age_seconds"], 60.0)

    def test_stale_scan_is_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make_context(tmp)
            state = ensure_state(ctx)
            state["runtime"]["last_scan_completed_at"] = "2026-08-03T10:00:00+00:00"

            payload, status = health_snapshot(
                ctx,
                state,
                now=dt.datetime(2026, 8, 3, 10, 4, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(status, 503)
            self.assertEqual(payload["status"], "degraded")

    def test_disconnected_demo_broker_is_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make_context(tmp, mode="demo")
            state = ensure_state(ctx)
            state["runtime"]["last_scan_completed_at"] = "2026-08-03T10:00:00+00:00"
            state["broker_status"] = {"connected": False}

            payload, status = health_snapshot(
                ctx,
                state,
                now=dt.datetime(2026, 8, 3, 10, 1, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(status, 503)
            self.assertFalse(payload["broker_connected"])

    def test_demo_orders_require_a_fresh_protection_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make_context(tmp, mode="demo")
            ctx.broker = FakeBroker()
            ctx.orders_enabled = True
            state = ensure_state(ctx)
            state["runtime"]["last_scan_completed_at"] = "2026-08-03T10:00:00+00:00"
            state["broker_status"] = {"connected": True}

            payload, status = health_snapshot(
                ctx,
                state,
                now=dt.datetime(2026, 8, 3, 10, 1, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(status, 503)
            self.assertTrue(payload["watchdog_required"])

            state["runtime"]["last_watchdog_completed_at"] = "2026-08-03T10:00:58+00:00"
            payload, status = health_snapshot(
                ctx,
                state,
                now=dt.datetime(2026, 8, 3, 10, 1, tzinfo=dt.timezone.utc),
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["watchdog_age_seconds"], 2.0)

    def test_active_watchdog_uses_its_start_time_for_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.make_context(tmp, mode="demo")
            ctx.broker = FakeBroker()
            ctx.orders_enabled = True
            state = ensure_state(ctx)
            state["runtime"].update({
                "last_scan_completed_at": "2026-08-03T10:00:00+00:00",
                "last_watchdog_completed_at": "2026-08-03T10:00:45+00:00",
                "last_watchdog_started_at": "2026-08-03T10:00:58+00:00",
                "watchdog_in_progress": True,
            })
            state["broker_status"] = {"connected": True}

            payload, status = health_snapshot(
                ctx,
                state,
                now=dt.datetime(2026, 8, 3, 10, 1, tzinfo=dt.timezone.utc),
            )

            self.assertEqual(status, 200)
            self.assertEqual(payload["watchdog_age_seconds"], 2.0)


class BotModeTests(unittest.TestCase):
    def test_binance_market_candles_are_fetched_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "paper")
            config["app"]["market_fetch_workers"] = 3
            config["market"]["symbols"] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            entered = 0
            entered_lock = threading.Lock()
            all_entered = threading.Event()
            release = threading.Event()

            def concurrent_fetch(_ctx, _symbol):
                nonlocal entered
                with entered_lock:
                    entered += 1
                    if entered == 3:
                        all_entered.set()
                release.wait(1)
                return [Candle(0, 99, 101, 98, 100, 10, 899_999)]

            def scanned(_ctx, _state, symbol, _btc, _candles):
                return {"symbol": symbol, "status": "no signal", "candle_open_time": 0}

            with (
                patch("crypto_autobot.bot.fetch_market_candles", side_effect=concurrent_fetch),
                patch("crypto_autobot.bot.scan_symbol", side_effect=scanned),
            ):
                scan_thread = threading.Thread(target=scan_once, args=(ctx,))
                scan_thread.start()
                self.assertTrue(all_entered.wait(0.5))
                release.set()
                scan_thread.join(2)

            self.assertFalse(scan_thread.is_alive())
            self.assertEqual(entered, 3)

    def test_execution_diagnostics_count_each_symbol_candle_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = BotContext(
                config=mode_config(tmp, "paper"),
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            first = {
                "symbol": "BTCUSDT",
                "candle_open_time": 900_000,
                "status": "ADX filter: 14.6",
            }
            signal = {
                "symbol": "BTCUSDT",
                "candle_open_time": 1_800_000,
                "status": "placed short limit",
            }

            record_scan_diagnostic(state, first, ctx.timezone)
            record_scan_diagnostic(state, first, ctx.timezone)
            record_scan_diagnostic(state, signal, ctx.timezone)

            diagnostics = state["execution_diagnostics"]
            self.assertEqual(diagnostics["candles_observed"], 2)
            self.assertEqual(diagnostics["status_counts"], {"no_signal": 1, "signal_order": 1})
            self.assertEqual(state["validation_coverage"]["1970-01-01"]["symbol_candles"], 2)

    def test_validation_coverage_ignores_stale_data_and_scan_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = BotContext(
                config=mode_config(tmp, "paper"),
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)

            record_scan_diagnostic(
                state,
                {"symbol": "BTCUSDT", "candle_open_time": 900_000, "status": "stale market data"},
                ctx.timezone,
            )
            record_scan_diagnostic(
                state,
                {"symbol": "ETHUSDT", "candle_open_time": 900_000, "status": "scan error: timeout"},
                ctx.timezone,
            )

            self.assertEqual(state["validation_coverage"], {})
            self.assertEqual(
                state["execution_diagnostics"]["status_counts"],
                {"stale_data": 1, "error": 1},
            )

    def test_daily_validation_metrics_survive_trade_log_trimming(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = BotContext(
                config=mode_config(tmp, "paper"),
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            ensure_trades_file(ctx)
            day = state["daily"].setdefault("2026-08-01", {"trades": 300, "validation_trades": 300})

            for index in range(300):
                base = {
                    "time": "2026-08-01T12:00:00+00:00",
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "source": "baseline",
                }
                append_trade(ctx, state, {**base, "event": "open", "pnl": 0, "reason": str(index)})
                append_trade(ctx, state, {**base, "event": "close", "pnl": 3.0, "reason": str(index)})
            append_trade(
                ctx,
                state,
                {
                    "time": "2026-08-01T12:00:00+00:00",
                    "event": "close",
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "pnl": 999.0,
                    "source": "manual_demo_test",
                },
            )

            self.assertEqual(len(state["trades"]), 500)
            self.assertEqual(day["validation_closed"], 300)
            self.assertEqual(len(day["validation_pnls"]), 300)
            self.assertEqual(day["validation_realized_pnl"], 900.0)

    def test_daily_validation_history_migrates_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = BotContext(
                config=mode_config(tmp, "paper"),
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            legacy = {
                "initial_balance": 1000,
                "daily": {"2026-08-01": {"trades": 2, "validation_trades": 1}},
                "trades": [
                    {
                        "time": "2026-08-02T00:30:00+00:00",
                        "event": "close",
                        "pnl": 3.0,
                        "validation_date": "2026-08-01",
                    },
                    {
                        "time": "2026-08-01T13:00:00+00:00",
                        "event": "close",
                        "pnl": 50.0,
                        "source": "manual_demo_test",
                    },
                ],
            }
            ctx.state_path.write_text(json.dumps(legacy), encoding="utf-8")

            migrated = ensure_state(ctx)
            write_state(ctx, migrated)
            reloaded = ensure_state(ctx)

            self.assertEqual(reloaded["validation_daily_version"], 1)
            self.assertEqual(reloaded["daily"]["2026-08-01"]["validation_pnls"], [3.0])
            self.assertEqual(reloaded["daily"]["2026-08-01"]["validation_closed"], 1)

    def test_limit_lifecycle_updates_execution_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            config["market"]["interval"] = "15m"
            config["strategy"].update(
                {"entry_order_type": "limit_retrace", "entry_offset_atr": 0.1, "entry_expiry_bars": 1}
            )
            broker = FakeBroker()
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            ensure_trades_file(ctx)
            signal = Candle(0, 99, 101, 98, 100, 10, 899_999)
            eligible = Candle(900_000, 100, 101, 99, 100, 10, 1_799_999)

            place_pending_entry(ctx, state, "BTCUSDT", "long", signal, 2.0, "test")
            reconcile_pending_entry(ctx, state, "BTCUSDT", [signal, eligible])

            diagnostics = state["execution_diagnostics"]
            self.assertEqual(diagnostics["signal_orders"], 1)
            self.assertEqual(diagnostics["limit_fills"], 1)
            self.assertEqual(diagnostics["limit_expired"], 0)

    def test_slow_account_refresh_does_not_block_protection_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            broker = FakeBroker()
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            refresh_started = threading.Event()
            release_refresh = threading.Event()
            original_summary = broker.account_summary

            def selective_summary():
                if threading.current_thread().name == "slow-scan":
                    refresh_started.set()
                    release_refresh.wait(1)
                return original_summary()

            broker.account_summary = selective_summary
            with patch(
                "crypto_autobot.bot.fetch_market_candles",
                side_effect=TimeoutError("stop after account refresh"),
            ):
                scan_thread = threading.Thread(
                    target=scan_once,
                    args=(ctx,),
                    name="slow-scan",
                )
                scan_thread.start()
                self.assertTrue(refresh_started.wait(0.5))

                watchdog = broker_watchdog_once(ctx)
                self.assertEqual(watchdog["status"], "ok")

                release_refresh.set()
                scan_thread.join(2)
                self.assertFalse(scan_thread.is_alive())

    def test_slow_market_fetch_does_not_block_protection_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            broker = FakeBroker()
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            fetch_started = threading.Event()
            release_fetch = threading.Event()

            def slow_fetch(*_args, **_kwargs):
                fetch_started.set()
                release_fetch.wait(1)
                raise TimeoutError("simulated slow market feed")

            with patch("crypto_autobot.bot.fetch_market_candles", side_effect=slow_fetch):
                scan_thread = threading.Thread(target=scan_once, args=(ctx,))
                scan_thread.start()
                self.assertTrue(fetch_started.wait(0.5))

                watchdog = broker_watchdog_once(ctx)
                self.assertEqual(watchdog["status"], "ok")
                self.assertIsNotNone(
                    ensure_state(ctx)["runtime"].get("last_watchdog_completed_at")
                )

                release_fetch.set()
                scan_thread.join(2)
                self.assertFalse(scan_thread.is_alive())

    def test_asymmetric_profile_enables_both_sides_with_smaller_long_risk(self):
        path = Path(__file__).parents[1] / "config.paper.asymmetric-15m.example.json"
        config = load_config(path)

        self.assertTrue(config["strategy"]["allow_longs"])
        self.assertTrue(config["strategy"]["allow_shorts"])
        self.assertFalse(config["ensemble"]["enabled"])
        self.assertEqual(config["account"]["short_risk_per_trade_percent"], 0.15)
        self.assertEqual(config["account"]["long_risk_per_trade_percent"], 0.025)
        self.assertEqual(config["market"]["interval"], "15m")

    def test_market_data_freshness_blocks_old_candles(self):
        interval_ms = 15 * 60 * 1000
        now_ms = 1_800_000_000_000
        fresh = Candle(0, 99, 101, 98, 100, 10, now_ms - interval_ms)
        stale = Candle(0, 99, 101, 98, 100, 10, now_ms - 2 * interval_ms - 1)

        self.assertTrue(market_data_is_fresh(fresh, "15m", 2, now_ms))
        self.assertFalse(market_data_is_fresh(stale, "15m", 2, now_ms))
        self.assertTrue(market_data_is_fresh(stale, "15m", 0, now_ms))

    def test_broker_readiness_checks_every_symbol_without_sending_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            config["market"].update({
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "interval": "15m",
                "history_limit": 50,
                "max_candle_age_intervals": 2,
            })
            broker = FakeBroker()
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=False,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            now_ms = int(time.time() * 1000)
            candles = [
                Candle(
                    open_time=now_ms - (40 - index) * 900_000,
                    open=100,
                    high=101,
                    low=99,
                    close=100,
                    volume=10,
                    close_time=now_ms - (39 - index) * 900_000 - 1,
                )
                for index in range(40)
            ]

            with patch("crypto_autobot.bot.fetch_market_candles", return_value=candles) as fetch:
                result = broker_readiness_snapshot(ctx)

            self.assertTrue(result["ready"])
            self.assertEqual(result["orders_sent"], 0)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual({item["symbol"] for item in result["symbols"]}, {"BTCUSDT", "ETHUSDT"})
            self.assertIsNone(broker.last_open_kwargs)

    def test_side_specific_risk_changes_paper_position_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            config["account"].update(
                {
                    "short_risk_per_trade_percent": 0.15,
                    "long_risk_per_trade_percent": 0.025,
                }
            )
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            ensure_trades_file(ctx)
            signal = Candle(0, 99, 101, 98, 100, 10, 899_999)

            open_position(ctx, state, "BTCUSDT", "long", signal, 10.0, "long test")
            long_position = state["positions"].pop("BTCUSDT")
            open_position(ctx, state, "BTCUSDT", "short", signal, 10.0, "short test")
            short_position = state["positions"]["BTCUSDT"]

            self.assertEqual(long_position["risk_percent"], 0.025)
            self.assertEqual(short_position["risk_percent"], 0.15)
            self.assertAlmostEqual(short_position["qty"] / long_position["qty"], 6.0)

    def test_side_specific_risk_is_passed_to_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(tmp)
            config["broker"] = {"mode": "demo", "provider": "binance"}
            config["account"]["long_risk_per_trade_percent"] = 0.025
            broker = FakeBroker()
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            ensure_trades_file(ctx)
            signal = Candle(0, 99, 101, 98, 100, 10, 899_999)

            open_position(ctx, state, "BTCUSDT", "long", signal, 2.0, "test")

            self.assertEqual(broker.last_open_kwargs["risk_percent"], 0.025)

    def test_watchdog_activates_a_filled_limit_without_waiting_for_market_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            config["market"]["interval"] = "15m"
            broker = FakeBroker()
            broker.order_status = "FILLED"
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            state["pending_entries"]["BTCUSDT"] = {
                "symbol": "BTCUSDT",
                "side": "long",
                "limit_price": 100.0,
                "stop_distance": 4.0,
                "target_distance": 6.0,
                "signal_candle_time": 1_700_000_000_000,
                "expiry_open_time": 9_000_000_000_000,
                "atr": 2.0,
                "reason": "watchdog test",
                "trade_profile": {},
                "entry_client_order_id": "autobot-limit-test",
            }
            write_state(ctx, state)

            result = broker_watchdog_once(ctx)
            state = ensure_state(ctx)

            self.assertEqual(result["status"], "ok")
            self.assertNotIn("BTCUSDT", state["pending_entries"])
            self.assertEqual(state["positions"]["BTCUSDT"]["stop"], 95.8)
            self.assertEqual(state["positions"]["BTCUSDT"]["target"], 105.8)
            self.assertEqual(len(broker.activation_calls), 1)

    def test_live_limit_keeps_its_full_retrace_candle_before_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            config["market"]["interval"] = "15m"
            broker = FakeBroker()
            broker.order_status = "NEW"
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            state["pending_entries"]["APTUSDT"] = {
                "symbol": "APTUSDT",
                "side": "long",
                "limit_price": 0.5742,
                "stop_distance": 0.004,
                "target_distance": 0.006,
                "signal_candle_time": 0,
                "expiry_open_time": 900_000,
                "atr": 0.002,
                "reason": "APT regression",
                "trade_profile": {},
                "entry_client_order_id": "autobot-limit-apt",
            }

            eligible_open = Candle(
                900_000, 0.575, 0.576, 0.575, 0.5755, 1, 1_799_999
            )
            status = reconcile_pending_entry(ctx, state, "APTUSDT", [eligible_open])

            self.assertEqual(status, "limit pending at 0.5742")
            self.assertIn("APTUSDT", state["pending_entries"])
            self.assertEqual(broker.order_status, "NEW")

            following_open = Candle(
                1_800_000, 0.576, 0.577, 0.575, 0.576, 1, 2_699_999
            )
            status = reconcile_pending_entry(ctx, state, "APTUSDT", [following_open])

            self.assertEqual(status, "limit entry expired")
            self.assertNotIn("APTUSDT", state["pending_entries"])
            self.assertEqual(broker.order_status, "CANCELED")
            self.assertEqual(state["execution_diagnostics"]["limit_expired"], 1)

    def test_watchdog_position_snapshot_skips_full_account_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            broker = FakeBroker()
            broker.account_summary = lambda: (_ for _ in ()).throw(
                AssertionError("watchdog must not run the full account refresh")
            )
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )

            result = broker_watchdog_once(ctx)

            self.assertEqual(result["status"], "ok")
            self.assertTrue(ensure_state(ctx)["broker_status"]["connected"])

    def test_watchdog_position_snapshot_failure_is_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            broker = FakeBroker()
            broker.get_open_positions = lambda: (_ for _ in ()).throw(
                TimeoutError("position snapshot timeout")
            )
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )

            result = broker_watchdog_once(ctx)
            state = ensure_state(ctx)

            self.assertEqual(result["status"], "degraded")
            self.assertFalse(state["broker_status"]["connected"])
            self.assertEqual(state["runtime"]["last_watchdog_errors"], 1)

    def test_watchdog_emergency_closes_a_position_missing_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            broker = FakeBroker()
            broker.protection_ok = False
            broker.exchange_positions = [{
                "symbol": "BTCUSDT",
                "side": "long",
                "quantity": 0.25,
                "entry": 100.0,
                "stop": 96.0,
                "target": 106.0,
                "unrealized_pnl": 0.0,
            }]
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            state["positions"]["BTCUSDT"] = {
                "symbol": "BTCUSDT",
                "side": "long",
                "entry": 100.0,
                "qty": 0.25,
                "stop": 96.0,
                "target": 106.0,
            }
            write_state(ctx, state)

            broker_watchdog_once(ctx)
            state = ensure_state(ctx)

            self.assertTrue(state["positions"]["BTCUSDT"]["emergency_close_sent"])
            self.assertEqual(len(broker.market_closes), 1)

    def test_config_can_inherit_a_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "base.json").write_text(
                json.dumps({"app": {"port": 1, "name": "base"}, "market": {"interval": "15m"}}),
                encoding="utf-8",
            )
            child = root / "child.json"
            child.write_text(
                json.dumps({"extends": "base.json", "app": {"name": "child"}}),
                encoding="utf-8",
            )

            config = load_config(child)

            self.assertEqual(config["app"], {"port": 1, "name": "child"})
            self.assertEqual(config["market"]["interval"], "15m")

    @patch("crypto_autobot.bot.MT5Broker")
    def test_build_context_supports_mt5_without_storing_credentials(self, mt5_class):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            config["broker"].update(
                {
                    "provider": "mt5",
                    "symbol_map": {"ETHUSDT": "ETHUSD"},
                    "magic": 12345,
                }
            )
            path = Path(tmp) / "config.mt5-demo.json"
            path.write_text(json.dumps(config), encoding="utf-8")

            with patch.dict(
                "os.environ",
                {
                    "MT5_LOGIN": "123456",
                    "MT5_PASSWORD": "private-password",
                    "MT5_SERVER": "Broker-Demo",
                },
                clear=False,
            ):
                ctx = build_context(path, orders_enabled=True)

            self.assertEqual(ctx.state_path.name, "state_mt5_demo.json")
            self.assertEqual(ctx.trades_path.name, "trades_mt5_demo.csv")
            self.assertTrue(ctx.orders_enabled)
            kwargs = mt5_class.call_args.kwargs
            self.assertEqual(kwargs["login"], 123456)
            self.assertEqual(kwargs["password"], "private-password")
            self.assertEqual(kwargs["server"], "Broker-Demo")
            self.assertEqual(kwargs["symbol_map"], {"ETHUSDT": "ETHUSD"})

    @patch("crypto_autobot.bot.request_json")
    def test_demo_klines_use_futures_path_and_keep_order_flow(self, request_json_mock):
        request_json_mock.return_value = [[
            0, "100", "102", "99", "101", "20", 899_999,
            "2020", 42, "12", "1212", "0",
        ]]

        candles = fetch_klines("https://demo-fapi.binance.com", "BTCUSDT", "15m", 10)

        self.assertEqual(request_json_mock.call_args.args[0], "https://demo-fapi.binance.com/fapi/v1/klines")
        self.assertEqual(candles[0].trade_count, 42)
        self.assertEqual(candles[0].taker_buy_volume, 12.0)
        self.assertEqual(candles[0].taker_buy_quote_volume, 1212.0)

    def test_mt5_profile_uses_broker_native_candles(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            config["broker"]["provider"] = "mt5"
            broker = FakeBroker()
            broker.fetch_candles = lambda symbol, interval, limit: [{
                "open_time": 1_700_000_000_000,
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 50,
                "close_time": 1_700_000_899_999,
            }]
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=False,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )

            with patch("crypto_autobot.bot.fetch_klines") as binance_fetch:
                candles = fetch_market_candles(ctx, "BTCUSDT")

            binance_fetch.assert_not_called()
            self.assertEqual(candles[0].close, 101.0)
            self.assertEqual(candles[0].open_dt, "2023-11-14 22:13 UTC")

    def test_public_state_hides_stale_market_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "paper")
            config["market"]["symbols"] = ["BTCUSDT"]
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_paper.json",
                trades_path=Path(tmp) / "trades_paper.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            state["latest"] = {
                "BTCUSDT": {"price": 100.0},
                "OLDUSDT": {"price": 10.0},
            }

            result = public_state(ctx, state)

            self.assertEqual(set(result["latest"]), {"BTCUSDT"})

    def test_paper_position_closes_after_max_holding_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "paper")
            config["market"]["interval"] = "15m"
            config["strategy"]["max_holding_bars"] = 2
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            ensure_trades_file(ctx)
            state["positions"]["BTCUSDT"] = {
                "symbol": "BTCUSDT",
                "side": "long",
                "entry": 100.0,
                "qty": 1.0,
                "stop": 90.0,
                "initial_stop": 90.0,
                "target": 120.0,
                "opened_candle_time": 0,
                "highest": 101.0,
                "lowest": 99.0,
            }

            manage_position(
                ctx,
                state,
                "BTCUSDT",
                Candle(1_800_000, 102, 104, 101, 103, 10, 2_699_999),
                None,
            )

            self.assertNotIn("BTCUSDT", state["positions"])
            self.assertEqual(state["trades"][-1]["reason"], "time_exit")

    def test_paper_limit_trade_includes_entry_and_target_maker_fees(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "paper")
            config["strategy"].update(
                {
                    "entry_order_type": "limit_retrace",
                    "target_order_type": "limit",
                    "stop_atr": 2.0,
                    "target_atr": 2.8,
                }
            )
            config["broker"].update(
                {
                    "paper_maker_fee_bps": 2.0,
                    "paper_taker_fee_bps": 5.0,
                    "paper_slippage_bps": 2.0,
                }
            )
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            ensure_trades_file(ctx)
            signal = Candle(0, 99, 101, 98, 100, 10, 899_999)

            open_position(ctx, state, "BTCUSDT", "long", signal, 10.0, "test")
            target = float(state["positions"]["BTCUSDT"]["target"])
            close_position(ctx, state, "BTCUSDT", target, "target")

            self.assertAlmostEqual(state["balance"], 1006.9886, places=4)
            self.assertAlmostEqual(state["realized_pnl"], 6.9886, places=4)

    def test_position_keeps_its_own_ml_rr_and_time_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "paper")
            config["market"]["interval"] = "15m"
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            ensure_trades_file(ctx)
            candle = Candle(0, 99, 101, 98, 100, 10, 899_999)

            open_position(
                ctx,
                state,
                "BTCUSDT",
                "long",
                candle,
                10.0,
                "ml test",
                trade_profile={
                    "source": "orderflow_ml",
                    "entry_order_type": "market",
                    "target_order_type": "limit",
                    "stop_atr": 1.5,
                    "target_atr": 3.0,
                    "max_holding_bars": 16,
                },
            )

            position = state["positions"]["BTCUSDT"]
            self.assertEqual(position["stop"], 85.0)
            self.assertEqual(position["target"], 130.0)
            self.assertEqual(position["source"], "orderflow_ml")
            self.assertEqual(position["max_holding_bars"], 16)

    def test_demo_time_exit_cancels_protection_before_market_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            config["market"]["interval"] = "15m"
            config["strategy"]["max_holding_bars"] = 2
            broker = FakeBroker()
            exchange_position = {
                "symbol": "BTCUSDT",
                "positionAmt": "0.25",
                "entryPrice": "100",
                "unRealizedProfit": "0.75",
            }
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={"BTCUSDT": exchange_position},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            state["broker_status"] = {"connected": True}
            state["positions"]["BTCUSDT"] = {
                "symbol": "BTCUSDT",
                "side": "long",
                "entry": 100.0,
                "qty": 0.25,
                "stop": 90.0,
                "initial_stop": 90.0,
                "target": 120.0,
                "opened_candle_time": 0,
            }

            manage_position(
                ctx,
                state,
                "BTCUSDT",
                Candle(1_800_000, 102, 104, 101, 103, 10, 2_699_999),
                None,
            )

            self.assertEqual(broker.cancelled_protection, ["BTCUSDT"])
            self.assertEqual(broker.market_closes, [("BTCUSDT", exchange_position)])
            self.assertTrue(state["positions"]["BTCUSDT"]["time_exit_sent"])

    def test_regime_profile_switches_to_matching_paper_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper_path = root / "config.paper.regime-scalp.example.json"
            demo_path = root / "config.demo.regime-scalp.example.json"
            paper_path.write_text(json.dumps(mode_config(tmp, "paper")), encoding="utf-8")
            demo_path.write_text(json.dumps(mode_config(tmp, "demo")), encoding="utf-8")
            paper = build_context(paper_path)
            controller = RuntimeController(
                paper,
                paper_path,
                orders_enabled=False,
                allow_live_ui=False,
            )

            self.assertEqual(controller.profile_paths["demo"], demo_path.resolve())
            self.assertFalse(controller.profile_paths["live"].exists())

    def test_filled_limit_becomes_a_protected_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            config["market"]["interval"] = "15m"
            config["strategy"].update(
                {"entry_order_type": "limit_retrace", "entry_offset_atr": 0.1, "entry_expiry_bars": 1}
            )
            broker = FakeBroker()
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            ensure_trades_file(ctx)
            signal = Candle(0, 99, 101, 98, 100, 10, 899_999)
            next_candle = Candle(900_000, 100, 101, 99, 100, 10, 1_799_999)
            place_pending_entry(ctx, state, "BTCUSDT", "long", signal, 2.0, "test signal")
            self.assertNotIn("BTCUSDT", state["positions"])
            self.assertIn("BTCUSDT", state["pending_entries"])

            result = reconcile_pending_entry(ctx, state, "BTCUSDT", [signal, next_candle])
            self.assertEqual(result, "limit filled long")
            self.assertNotIn("BTCUSDT", state["pending_entries"])
            self.assertEqual(state["positions"]["BTCUSDT"]["entry"], 99.8)
            self.assertEqual(state["positions"]["BTCUSDT"]["stop_algo_id"], 11)
            self.assertEqual(sum(int(item["trades"]) for item in state["daily"].values()), 1)

    def test_demo_test_order_is_blocked_outside_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = BotContext(
                config=mode_config(tmp, "paper"),
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            with self.assertRaisesRegex(ValueError, "only in Binance Demo"):
                open_demo_test_order(ctx, "BTCUSDT", "long", DEMO_TEST_CONFIRMATION)

    def test_zero_demo_balance_does_not_become_initial_balance(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=FakeBroker(),
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            state["balance"] = 0
            state["initial_balance"] = 0
            state["exchange_balance_initialized"] = True
            with patch.object(ctx.broker, "account_summary", create=True, return_value={
                "balance": 1000,
                "available_balance": 1000,
                "positions": [],
                "environment": "demo",
                "position_mode": "ONE_WAY",
            }):
                from crypto_autobot.bot import refresh_exchange_account
                refresh_exchange_account(ctx, state)
            self.assertEqual(state["initial_balance"], 1000)

    def test_confirmed_demo_test_order_opens_a_protected_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = mode_config(tmp, "demo")
            config["strategy"]["atr_length"] = 2
            broker = FakeBroker()
            ctx = BotContext(
                config=config,
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=broker,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            candles = [
                Candle(index, 99, 102, 98, 100, 10, index + 1)
                for index in range(4)
            ]
            summary = {
                "balance": 1000,
                "available_balance": 1000,
                "positions": [],
                "environment": "demo",
                "position_mode": "ONE_WAY",
            }
            with (
                patch.object(broker, "account_summary", create=True, return_value=summary),
                patch("crypto_autobot.bot.fetch_klines", return_value=candles),
            ):
                payload = open_demo_test_order(
                    ctx,
                    "BTCUSDT",
                    "long",
                    DEMO_TEST_CONFIRMATION,
                )
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["state"]["positions"]["BTCUSDT"]["side"], "long")
            self.assertEqual(payload["state"]["positions"]["BTCUSDT"]["reason"], "manual Binance Demo market test")
            self.assertEqual(payload["state"]["positions"]["BTCUSDT"]["source"], "manual_demo_test")
            state = ensure_state(ctx)
            self.assertEqual(next(iter(state["daily"].values()))["validation_trades"], 0)

    def test_adx_detects_a_clean_trend(self):
        candles = [
            Candle(
                open_time=index,
                open=100 + index,
                high=102 + index,
                low=99 + index,
                close=101 + index,
                volume=10,
                close_time=index + 1,
            )
            for index in range(60)
        ]
        values = adx(candles, 14)
        self.assertIsNotNone(values[-1])
        self.assertGreater(float(values[-1]), 50)

    def test_exchange_open_is_written_to_separate_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = BotContext(
                config=test_config(tmp),
                state_path=Path(tmp) / "state_demo.json",
                trades_path=Path(tmp) / "trades_demo.csv",
                timezone=ZoneInfo("UTC"),
                mode="demo",
                broker=FakeBroker(),
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            state = ensure_state(ctx)
            ensure_trades_file(ctx)
            candle = Candle(
                open_time=1,
                open=99,
                high=101,
                low=98,
                close=100,
                volume=10,
                close_time=2,
            )
            open_position(ctx, state, "BTCUSDT", "long", candle, 2.0, "test signal")
            position = state["positions"]["BTCUSDT"]
            self.assertEqual(position["mode"], "demo")
            self.assertEqual(position["entry"], 100.0)
            self.assertEqual(position["stop"], 96.0)
            self.assertEqual(position["target"], 106.0)
            self.assertEqual(position["entry_order_id"], 1)
            self.assertEqual(state["daily"][next(iter(state["daily"]))]["trades"], 1)

    def test_state_json_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = BotContext(
                config=test_config(tmp),
                state_path=Path(tmp) / "state.json",
                trades_path=Path(tmp) / "trades.csv",
                timezone=ZoneInfo("UTC"),
                mode="paper",
                broker=None,
                orders_enabled=True,
                exchange_snapshot={},
                lock=threading.Lock(),
                stop_event=threading.Event(),
            )
            ensure_state(ctx)
            payload = json.loads(ctx.state_path.read_text(encoding="utf-8"))
            text = json.dumps(payload).lower()
            self.assertNotIn("api_key", text)
            self.assertNotIn("api_secret", text)

    def test_runtime_switches_paper_to_demo_with_separate_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_mode_configs(Path(tmp), tmp)
            paper = build_context(paths["paper"])
            controller = RuntimeController(
                paper,
                paths["paper"],
                orders_enabled=True,
                allow_live_ui=False,
            )
            with patch.dict(
                "os.environ",
                {
                    "BINANCE_DEMO_API_KEY": "demo-key",
                    "BINANCE_DEMO_API_SECRET": "demo-secret",
                },
                clear=False,
            ):
                result = controller.switch_mode("demo")
            self.assertTrue(result["changed"])
            self.assertEqual(controller.current().mode, "demo")
            self.assertEqual(controller.current().state_path.name, "state_demo.json")
            self.assertTrue(controller.current().orders_enabled)

    def test_runtime_demo_requires_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_mode_configs(Path(tmp), tmp)
            paper = build_context(paths["paper"])
            controller = RuntimeController(
                paper,
                paths["paper"],
                orders_enabled=True,
                allow_live_ui=False,
            )
            with patch.dict(
                "os.environ",
                {"BINANCE_DEMO_API_KEY": "", "BINANCE_DEMO_API_SECRET": ""},
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "BINANCE_DEMO_API_KEY"):
                    controller.switch_mode("demo")

    def test_runtime_live_requires_unlock_and_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_mode_configs(Path(tmp), tmp)
            paper = build_context(paths["paper"])
            locked = RuntimeController(
                paper,
                paths["paper"],
                orders_enabled=True,
                allow_live_ui=False,
            )
            live_env = {
                "BINANCE_LIVE_API_KEY": "live-key",
                "BINANCE_LIVE_API_SECRET": "live-secret",
            }
            with patch.dict("os.environ", live_env, clear=False):
                with self.assertRaisesRegex(ValueError, "--allow-live-ui"):
                    locked.switch_mode("live", confirmation=LIVE_CONFIRMATION)

                unlocked = RuntimeController(
                    paper,
                    paths["paper"],
                    orders_enabled=True,
                    allow_live_ui=True,
                )
                seed_passing_demo_validation(tmp)
                with self.assertRaisesRegex(ValueError, "точную фразу"):
                    unlocked.switch_mode("live", confirmation="wrong")
                result = unlocked.switch_mode("live", confirmation=LIVE_CONFIRMATION)
            self.assertTrue(result["changed"])
            self.assertEqual(unlocked.current().mode, "live")

    def test_runtime_live_is_blocked_until_demo_forward_gate_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_mode_configs(Path(tmp), tmp)
            paper = build_context(paths["paper"])
            controller = RuntimeController(
                paper,
                paths["paper"],
                orders_enabled=True,
                allow_live_ui=True,
            )
            with patch.dict(
                "os.environ",
                {
                    "BINANCE_LIVE_API_KEY": "live-key",
                    "BINANCE_LIVE_API_SECRET": "live-secret",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "Live gate"):
                    controller.switch_mode("live", confirmation=LIVE_CONFIRMATION)

            report = controller.mode_control(is_local=True)["forward_validation"]
            self.assertEqual(report["status"], "collecting")
            self.assertFalse(report["ready_for_live"])

    def test_direct_live_runtime_cannot_bypass_forward_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_mode_configs(Path(tmp), tmp)
            with patch.dict(
                "os.environ",
                {
                    "BINANCE_LIVE_API_KEY": "live-key",
                    "BINANCE_LIVE_API_SECRET": "live-secret",
                },
                clear=False,
            ):
                live = build_context(
                    paths["live"],
                    orders_enabled=True,
                    live_confirmation=LIVE_CONFIRMATION,
                )
            controller = RuntimeController(
                live,
                paths["live"],
                orders_enabled=True,
                allow_live_ui=True,
            )
            with self.assertRaisesRegex(ValueError, "Live gate"):
                controller.require_live_forward_gate()

            seed_passing_demo_validation(tmp)
            report = controller.require_live_forward_gate()
            self.assertTrue(report["ready_for_live"])

    def test_runtime_does_not_abandon_an_open_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_mode_configs(Path(tmp), tmp)
            paper = build_context(paths["paper"])
            state = ensure_state(paper)
            state["positions"]["BTCUSDT"] = {"side": "long", "entry": 100, "qty": 1}
            write_state(paper, state)
            controller = RuntimeController(
                paper,
                paths["paper"],
                orders_enabled=True,
                allow_live_ui=False,
            )
            with patch.dict(
                "os.environ",
                {
                    "BINANCE_DEMO_API_KEY": "demo-key",
                    "BINANCE_DEMO_API_SECRET": "demo-secret",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "открытую позицию"):
                    controller.switch_mode("demo")

    def test_runtime_does_not_abandon_a_pending_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_mode_configs(Path(tmp), tmp)
            paper = build_context(paths["paper"])
            state = ensure_state(paper)
            state["pending_entries"]["BTCUSDT"] = {"side": "long", "limit_price": 99}
            write_state(paper, state)
            controller = RuntimeController(
                paper,
                paths["paper"],
                orders_enabled=True,
                allow_live_ui=False,
            )
            with patch.dict(
                "os.environ",
                {
                    "BINANCE_DEMO_API_KEY": "demo-key",
                    "BINANCE_DEMO_API_SECRET": "demo-secret",
                },
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "лимитная заявка"):
                    controller.switch_mode("demo")


if __name__ == "__main__":
    unittest.main()
