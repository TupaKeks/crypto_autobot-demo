from __future__ import annotations

import json
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from crypto_autobot.binance_futures import (
    BinanceFuturesBroker,
    LIVE_CONFIRMATION,
    floor_to_step,
    price_to_tick,
)
from crypto_autobot.bot import (
    BotContext,
    Candle,
    RuntimeController,
    adx,
    build_context,
    ensure_state,
    ensure_trades_file,
    open_position,
    write_state,
)


class FakeBroker:
    def open_position(self, **kwargs):
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


class BinanceMathTests(unittest.TestCase):
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
            {"orderType": "STOP_MARKET", "algoStatus": "NEW"},
            {"orderType": "TAKE_PROFIT_MARKET", "algoStatus": "NEW"},
        ]
        self.assertTrue(broker.has_stop_and_target("BTCUSDT"))
        broker.signed = lambda method, path, params=None: [
            {"orderType": "STOP_MARKET", "algoStatus": "NEW"}
        ]
        self.assertFalse(broker.has_stop_and_target("BTCUSDT"))


class BotModeTests(unittest.TestCase):
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
                with self.assertRaisesRegex(ValueError, "точную фразу"):
                    unlocked.switch_mode("live", confirmation="wrong")
                result = unlocked.switch_mode("live", confirmation=LIVE_CONFIRMATION)
            self.assertTrue(result["changed"])
            self.assertEqual(unlocked.current().mode, "live")

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


if __name__ == "__main__":
    unittest.main()
