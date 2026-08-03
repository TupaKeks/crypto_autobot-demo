from __future__ import annotations

import unittest
from types import SimpleNamespace

from crypto_autobot.mt5_broker import MT5Broker


class FakeMT5:
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    TRADE_ACTION_SLTP = 6
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2
    POSITION_TYPE_BUY = 0
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE_PARTIAL = 10010
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240
    TIMEFRAME_D1 = 1440

    def __init__(self):
        self.requests: list[dict] = []
        self.one_lot_loss = -10.0
        self.positions = []
        self.deals = []
        self.trade_mode = self.ACCOUNT_TRADE_MODE_DEMO
        self.rates = []
        self.rate_requests = []

    def initialize(self, *args, **kwargs):
        return True

    def shutdown(self):
        return None

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return SimpleNamespace(
            balance=10_000.0,
            margin_free=9_000.0,
            currency="USD",
            margin_mode=0,
            trade_mode=self.trade_mode,
        )

    def symbol_info(self, symbol):
        return SimpleNamespace(
            visible=True,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=10.0,
            digits=2,
            filling_mode=self.ORDER_FILLING_RETURN,
        )

    def symbol_select(self, symbol, enabled):
        return True

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(ask=100.0, bid=99.9)

    def positions_get(self, **kwargs):
        return tuple(self.positions)

    def orders_get(self, **kwargs):
        return ()

    def order_calc_profit(self, order_type, symbol, volume, entry, stop):
        return self.one_lot_loss

    def order_send(self, request):
        self.requests.append(dict(request))
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=123,
            price=request.get("price", 0.0),
            comment="done",
        )

    def history_deals_get(self, *args, **kwargs):
        return tuple(self.deals)

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        self.rate_requests.append((symbol, timeframe, start_pos, count))
        return tuple(self.rates[-count:])


class MT5BrokerTests(unittest.TestCase):
    def setUp(self):
        self.mt5 = FakeMT5()
        self.broker = MT5Broker(
            environment="demo",
            orders_enabled=True,
            symbol_map={"BTCUSDT": "BTCUSD"},
            mt5_module=self.mt5,
        )

    def test_limit_order_attaches_stop_and_target_before_submission(self):
        result = self.broker.place_limit_entry(
            symbol="BTCUSDT",
            side="long",
            limit_price=100.0,
            stop_distance=2.0,
            target_distance=4.0,
            risk_percent=0.15,
            max_open_positions=3,
        )

        request = self.mt5.requests[-1]
        self.assertEqual(request["action"], self.mt5.TRADE_ACTION_PENDING)
        self.assertEqual(request["symbol"], "BTCUSD")
        self.assertEqual(request["type"], self.mt5.ORDER_TYPE_BUY_LIMIT)
        self.assertEqual(request["sl"], 98.0)
        self.assertEqual(request["tp"], 104.0)
        self.assertEqual(request["volume"], 1.5)
        self.assertEqual(result["entry_client_order_id"], "123")
        self.assertEqual(result["wallet_balance"], 10_000.0)

    def test_fetch_candles_uses_mapped_symbol_and_only_closed_bars(self):
        self.mt5.rates = [
            {
                "time": 1_700_000_000,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "tick_volume": 50,
                "real_volume": 0,
            },
            {
                "time": 1_700_000_900,
                "open": 101.0,
                "high": 103.0,
                "low": 100.0,
                "close": 102.0,
                "tick_volume": 60,
                "real_volume": 12,
            },
        ]

        candles = self.broker.fetch_candles("BTCUSDT", "15m", 200)

        self.assertEqual(self.mt5.rate_requests, [("BTCUSD", self.mt5.TIMEFRAME_M15, 1, 200)])
        self.assertEqual([item["open_time"] for item in candles], [1_700_000_000_000, 1_700_000_900_000])
        self.assertEqual(candles[0]["volume"], 50.0)
        self.assertEqual(candles[1]["volume"], 12.0)
        self.assertEqual(candles[0]["close_time"], 1_700_000_899_999)

    def test_fetch_candles_rejects_an_unsupported_interval(self):
        with self.assertRaisesRegex(ValueError, "Unsupported MT5 candle interval"):
            self.broker.fetch_candles("BTCUSDT", "2h", 100)

    def test_position_and_realized_pnl_follow_runtime_contract(self):
        self.mt5.positions = [SimpleNamespace(
            symbol="BTCUSD",
            type=self.mt5.POSITION_TYPE_BUY,
            volume=0.25,
            price_open=100.0,
            sl=98.0,
            tp=104.0,
            profit=1.5,
            ticket=77,
            time=1_700_000_000,
            time_msc=1_700_000_000_000,
            magic=self.broker.magic,
        )]
        self.mt5.deals = [SimpleNamespace(
            magic=self.broker.magic,
            profit=10.0,
            commission=-0.8,
            swap=-0.2,
            fee=0.0,
        )]

        position = self.broker.get_open_position("BTCUSDT")
        pnl = self.broker.realized_pnl_since("BTCUSDT", 1_699_999_000_000)

        self.assertEqual(position["symbol"], "BTCUSDT")
        self.assertEqual(position["side"], "long")
        self.assertEqual(position["quantity"], 0.25)
        self.assertTrue(position["managed"])
        self.assertEqual(pnl, {"realized_pnl": 10.0, "commission": 1.0, "net_pnl": 9.0})

    def test_volume_below_broker_minimum_is_rejected(self):
        self.mt5.one_lot_loss = -10_000.0

        with self.assertRaisesRegex(ValueError, "below the broker minimum"):
            self.broker.open_position(
                symbol="BTCUSDT",
                side="long",
                stop_distance=2.0,
                target_distance=4.0,
                risk_percent=0.15,
                max_open_positions=3,
            )

    def test_orders_disabled_blocks_submission(self):
        broker = MT5Broker(
            environment="demo",
            orders_enabled=False,
            mt5_module=self.mt5,
        )

        with self.assertRaisesRegex(ValueError, "orders are disabled"):
            broker.place_limit_entry(
                symbol="BTCUSDT",
                side="short",
                limit_price=100.0,
                stop_distance=2.0,
                target_distance=4.0,
                risk_percent=0.15,
                max_open_positions=3,
            )

    def test_live_orders_require_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, "live order safety lock"):
            MT5Broker(
                environment="live",
                orders_enabled=True,
                mt5_module=self.mt5,
            )

    def test_demo_profile_rejects_a_real_money_account(self):
        self.mt5.trade_mode = self.mt5.ACCOUNT_TRADE_MODE_REAL

        with self.assertRaisesRegex(ValueError, "real-money account"):
            MT5Broker(
                environment="demo",
                orders_enabled=True,
                mt5_module=self.mt5,
            )


if __name__ == "__main__":
    unittest.main()
