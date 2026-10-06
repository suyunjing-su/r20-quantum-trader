"""Contract tests for the official Binance SDK adapter boundary."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from astra_backend.exchanges.binance import BinanceAdapter
from astra_backend.exchanges.base import ExchangeCapabilityError


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def data(self):
        return self.payload


class _RestAPI:
    def __init__(self):
        self.calls = []

    def ticker24hr_price_change_statistics(self, **kwargs):
        self.calls.append(("ticker24hr_price_change_statistics", kwargs))
        return _Response({
            "lastPrice": "100", "bidPrice": "99", "askPrice": "101",
            "openPrice": "98", "highPrice": "102", "lowPrice": "97",
            "priceChangePercent": "2", "volume": "3", "quoteVolume": "300",
            "closeTime": 123,
        })

    def symbol_order_book_ticker(self, **kwargs):
        self.calls.append(("symbol_order_book_ticker", kwargs))
        return _Response({"bidPrice": "99", "askPrice": "101"})

    def account_information_v2(self, **kwargs):
        self.calls.append(("account_information_v2", kwargs))
        return _Response({
            "totalMarginBalance": "10", "totalWalletBalance": "9",
            "availableBalance": "8", "canTrade": True,
        })

    def new_order(self, **kwargs):
        self.calls.append(("new_order", kwargs))
        return _Response({
            "orderId": 99, "clientOrderId": "client-1", "symbol": "BTCUSDT",
            "side": "BUY", "status": "NEW", "origQty": "0.1",
        })

    def new_algo_order(self, **kwargs):
        self.calls.append(("new_algo_order", kwargs))
        return _Response({"algoId": 100})

    def get_income_history(self, **kwargs):
        self.calls.append(("get_income_history", kwargs))
        return _Response([{"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "2"}])

    def account_trade_list(self, **kwargs):
        self.calls.append(("account_trade_list", kwargs))
        return _Response([{"symbol": "BTCUSDT", "qty": "0.1", "side": "BUY"}])

    def position_information_v2(self, **kwargs):
        self.calls.append(("position_information_v2", kwargs))
        return _Response([{"symbol": "BTCUSDT", "positionAmt": "0.1"}])

    def top_trader_long_short_ratio_positions(self, **kwargs):
        self.calls.append(("top_trader_long_short_ratio_positions", kwargs))
        return _Response([{"longAccount": "0.6", "longShortRatio": "1.5"}])

    def taker_buy_sell_volume(self, **kwargs):
        self.calls.append(("taker_buy_sell_volume", kwargs))
        return _Response([{"buyVol": "3", "sellVol": "2", "buySellRatio": "1.5"}])


class _Client:
    def __init__(self):
        self.rest_api = _RestAPI()


class BinanceSDKAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BinanceAdapter(environment="demo")
        self.client = _Client()
        self.adapter._sdk_client = self.client
        self.adapter._sdk_identity = ("", "", self.adapter.base_url)
        self.adapter._client = Mock(return_value=self.client)
        self.adapter._keys = Mock(return_value=("key", "secret"))

    def test_public_market_data_uses_generated_sdk_method(self):
        ticker = self.adapter.fetch_ticker("BTC")
        self.assertEqual(ticker["last"], 100.0)
        self.assertEqual(ticker["bid"], 99.0)
        self.assertEqual(
            self.client.rest_api.calls[0],
            ("ticker24hr_price_change_statistics", {"symbol": "BTCUSDT"}),
        )

    def test_public_ratio_helpers_use_generated_sdk_enums(self):
        top = self.adapter.fetch_top_trader_position_ratio("BTC", period="5m")
        taker = self.adapter.fetch_taker_volume("BTC", period="5m")
        self.assertEqual(top[0]["longAccount"], "0.6")
        self.assertEqual(taker[0]["buyVol"], "3")
        top_call = self.client.rest_api.calls[-2]
        taker_call = self.client.rest_api.calls[-1]
        self.assertEqual(top_call[0], "top_trader_long_short_ratio_positions")
        self.assertEqual(top_call[1]["symbol"], "BTCUSDT")
        self.assertEqual(top_call[1]["period"].value, "5m")
        self.assertEqual(taker_call[0], "taker_buy_sell_volume")
        self.assertEqual(taker_call[1]["period"].value, "5m")

    def test_private_account_uses_sdk_and_normalizes_response(self):
        account = self.adapter.account_snapshot()
        self.assertEqual(account["equity_usdt"], 10.0)
        self.assertEqual(account["available_usdt"], 8.0)
        self.assertEqual(self.client.rest_api.calls[0][0], "account_information_v2")
        self.adapter._keys.assert_called_once_with()

    def test_order_and_algo_calls_use_sdk_enums_not_request_dicts(self):
        spec = SimpleNamespace(
            step_size=0.001,
            tick_size=0.1,
            min_size=0.001,
            raw={"filters": [{"filterType": "MIN_NOTIONAL", "notional": "5"}]},
        )
        self.adapter.fetch_instrument_spec = Mock(return_value=spec)
        order = self.adapter.place_order("BTC", "buy", 0.1, price=100)
        self.assertEqual(order["order_id"], "99")
        order_call = self.client.rest_api.calls[-1]
        self.assertEqual(order_call[0], "new_order")
        self.assertEqual(order_call[1]["symbol"], "BTCUSDT")
        self.assertEqual(order_call[1]["quantity"], 0.1)

        legs = self.adapter.attach_protective_orders("BTC", "long", tp_px=110)
        self.assertEqual(legs["tp"], "100")
        self.assertEqual(legs["sl"], "")
        self.assertEqual(self.client.rest_api.calls[-1][0], "new_algo_order")

    def test_private_operations_fail_closed_without_credentials(self):
        self.adapter._keys = Mock(side_effect=ExchangeCapabilityError("未配置"))
        with self.assertRaises(ExchangeCapabilityError):
            self.adapter.account_snapshot()

    def test_history_and_position_risk_use_generated_sdk_methods(self):
        income = self.adapter.fetch_income_history(income_type="REALIZED_PNL", limit=100)
        trades = self.adapter.fetch_account_trades("BTC", limit=50)
        risk = self.adapter.fetch_position_risk()
        self.assertEqual(income[0]["income"], "2")
        self.assertEqual(trades[0]["side"], "BUY")
        self.assertEqual(risk[0]["positionAmt"], "0.1")
        self.assertEqual(
            [name for name, _ in self.client.rest_api.calls[-3:]],
            ["get_income_history", "account_trade_list", "position_information_v2"],
        )
        income_call = self.client.rest_api.calls[-3][1]
        self.assertEqual(income_call["symbol"], None)
        self.assertEqual(income_call["limit"], 100)
        self.assertEqual(income_call["income_type"].value, "REALIZED_PNL")
        self.assertEqual(self.client.rest_api.calls[-2][1]["symbol"], "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
