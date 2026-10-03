from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import market_data_service as mds


class _Adapter:
    def __init__(self, venue, ticker=None, candles=None):
        self.venue = venue
        self.ticker = ticker or {"last": 1.0, "bid": 0.9, "ask": 1.1, "chg_24h_pct": 0}
        self.candles = candles or {
            "15m": [[i, "1", "2", "0.5", "1", "10"] for i in range(24)],
            "1H": [[i, "1", "2", "0.5", "1", "10"] for i in range(24)],
            "4H": [[i, "1", "2", "0.5", "1", "10"] for i in range(16)],
        }
        self.calls = []

    def fetch_ticker(self, symbol):
        self.calls.append(("ticker", symbol))
        return self.ticker

    def fetch_candles(self, symbol, bar, limit):
        self.calls.append((bar, symbol))
        return self.candles.get(bar, [])[-limit:]

    def fetch_funding_rate(self, symbol):
        self.calls.append(("funding", symbol))
        return 0.0001


class FetchMarketBundleRoutingTests(unittest.TestCase):
    def setUp(self):
        self.adapters = {
            "okx": _Adapter("okx"),
            "binance": _Adapter("binance"),
            "gate": _Adapter("gate"),
        }

    def _fetch(self, venues):
        with patch.object(mds, "_get_venue_adapter", side_effect=lambda venue: self.adapters[venue]):
            return mds.fetch_market_bundle("1000PEPE-USDT-SWAP", venues)

    def test_preference_is_okx_then_binance_then_gate(self):
        bundle = self._fetch(["gate", "binance", "okx"])
        self.assertEqual(bundle["venue"], "okx")
        self.assertTrue(self.adapters["okx"].calls)
        self.assertEqual(self.adapters["binance"].calls, [])
        self.assertEqual(self.adapters["gate"].calls, [])

    def test_unpermitted_okx_is_removed_from_fallback_chain(self):
        bundle = self._fetch(["gate", "binance"])
        self.assertEqual(bundle["venue"], "binance")
        self.assertEqual(self.adapters["okx"].calls, [])
        self.assertEqual(self.adapters["gate"].calls, [])

    def test_missing_okx_contract_falls_back_to_binance(self):
        self.adapters["okx"].ticker = None
        bundle = self._fetch(["okx", "binance", "gate"])
        self.assertEqual(bundle["venue"], "binance")
        self.assertEqual(self.adapters["gate"].calls, [])

    def test_incomplete_first_venue_falls_back_without_mixing_candles(self):
        self.adapters["okx"].candles["4H"] = [[i, "1", "2", "0.5", "1", "10"] for i in range(3)]
        bundle = self._fetch(["okx", "binance", "gate"])
        self.assertEqual(bundle["venue"], "binance")
        self.assertEqual(len(bundle["candles"]["4H"]), 16)
        self.assertEqual(self.adapters["gate"].calls, [])

    def test_empty_allowlist_does_not_query_any_venue(self):
        self.assertIsNone(self._fetch([]))
        self.assertTrue(all(not adapter.calls for adapter in self.adapters.values()))

    def test_invalid_first_venue_ticker_falls_back(self):
        self.adapters["okx"].ticker = {"last": 1.0, "bid": 0.9, "ask": 0}
        bundle = self._fetch(["okx", "binance"])
        self.assertEqual(bundle["venue"], "binance")
        self.assertEqual(self.adapters["okx"].calls, [("ticker", "1000PEPE")])

    def test_selected_bundle_keeps_one_venue_candles_and_reverses_order(self):
        bundle = self._fetch(["binance"])
        self.assertEqual(bundle["venue"], "binance")
        self.assertEqual(bundle["candles"]["15m"][0][0], 23)
        self.assertEqual(bundle["candles"]["15m"][-1][0], 0)
        self.assertTrue(all(call[1] == "1000PEPE" for call in self.adapters["binance"].calls
                            if call[0] != "funding"))

    def test_single_route_uses_only_binance_as_market_reference(self):
        with patch("astra_backend.exchanges.routing_policy.load_multi_venue_routing_enabled", return_value=False),                 patch("astra_backend.exchanges.routing_policy.active_execution_venue", return_value="binance"):
            bundle = self._fetch(["okx", "binance", "gate"])
        self.assertEqual(bundle["venue"], "binance")
        self.assertEqual(self.adapters["okx"].calls, [])
        self.assertEqual(self.adapters["gate"].calls, [])
        self.assertTrue(self.adapters["binance"].calls)

    def test_single_route_conflict_fails_closed_without_guessing_okx(self):
        with patch("astra_backend.exchanges.routing_policy.load_multi_venue_routing_enabled", return_value=False),                 patch("astra_backend.exchanges.routing_policy.active_execution_venue", return_value=None):
            self.assertIsNone(self._fetch(["okx", "binance", "gate"]))
        self.assertTrue(all(not adapter.calls for adapter in self.adapters.values()))

    def test_all_allowed_venues_fail_returns_none(self):
        for adapter in self.adapters.values():
            adapter.ticker = None
        self.assertIsNone(self._fetch(["okx", "binance", "gate"]))
        self.assertTrue(all(adapter.calls == [("ticker", "1000PEPE")] for adapter in self.adapters.values()))


if __name__ == "__main__":
    unittest.main()
