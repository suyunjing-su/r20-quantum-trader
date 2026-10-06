"""`scripts/factors/smart_money.py` 单元测试与双源容灾验证。"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from scripts.factors.smart_money import (
    fetch_smart_money_for_symbol,
    fetch_smart_money_pool,
    _fetch_from_binance,
    _fetch_from_okx_rubik,
)


class TestSmartMoneyExtraction(unittest.TestCase):
    def test_empty_symbol(self):
        self.assertIsNone(fetch_smart_money_for_symbol(""))
        self.assertIsNone(fetch_smart_money_for_symbol(None))  # type: ignore

    def test_binance_success(self):
        adapter = MagicMock()
        adapter.fetch_top_trader_position_ratio.return_value = [
            {"symbol": "SOLUSDT", "longAccount": "0.6850",
             "shortAccount": "0.3150", "longShortRatio": "2.1746"}]
        adapter.fetch_taker_volume.return_value = [
            {"buyVol": "10000", "sellVol": "5000", "buySellRatio": "2.0"}]
        with patch("scripts.factors.smart_money.get_adapter", return_value=adapter):
            res = _fetch_from_binance("SOL", price=100.0)
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res["longShortRatio"]["weightedLongRatio"], 0.6850)
        self.assertAlmostEqual(res["longShortRatio"]["longShortRatio"], 2.1746)
        self.assertEqual(res["weighted_long_pct"], 68.5)
        self.assertIn("万 U", res["takerNetUsd"])
        self.assertAlmostEqual(res["notional"]["netNotionalUsdt"], 500000.0)

    @patch("scripts.factors.smart_money._fetch_from_binance", return_value=None)
    @patch("scripts.factors.smart_money._fetch_from_okx_rubik")
    def test_fallback_to_okx_when_binance_fails(self, mock_okx, mock_bin):
        mock_okx.return_value = {
            "longShortRatio": {"weightedLongRatio": 0.60, "longShortRatio": 1.5},
            "notional": {"netNotionalUsdt": 12000.0},
            "winRate": {},
            "takerNetUsd": "1.2万 U",
            "lsRatio": 1.5,
            "weighted_long_pct": 60.0,
        }
        res = fetch_smart_money_for_symbol("SOL")
        self.assertIsNotNone(res)
        self.assertEqual(res["weighted_long_pct"], 60.0)
        self.assertEqual(res["takerNetUsd"], "1.2万 U")

    @patch("scripts.factors.smart_money._fetch_from_binance", return_value=None)
    @patch("scripts.factors.smart_money._fetch_from_okx_rubik", return_value=None)
    def test_graceful_none_when_both_fail(self, mock_okx, mock_bin):
        res = fetch_smart_money_for_symbol("SOL")
        self.assertIsNone(res)

    @patch("scripts.factors.smart_money.fetch_smart_money_for_symbol")
    def test_fetch_smart_money_pool(self, mock_fetch):
        mock_fetch.side_effect = lambda ccy, **kw: {
            "longShortRatio": {"weightedLongRatio": 0.65},
            "notional": {"netNotionalUsdt": 10000},
            "winRate": {},
            "weighted_long_pct": 65.0,
        } if ccy == "SOL" else None

        instruments = [
            {"name": "SOL", "ccy": "SOL", "price": 106.0},
            {"name": "UNKNOWN", "ccy": "UNKNOWN", "price": 1.0},
        ]
        pool = fetch_smart_money_pool(instruments)
        self.assertIn("SOL", pool)
        self.assertNotIn("UNKNOWN", pool)
        self.assertEqual(pool["SOL"]["weighted_long_pct"], 65.0)


if __name__ == "__main__":
    unittest.main()
