import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astra_backend.instrument_metadata import (
    add_native_specs,
    canonical_pool_metadata,
    fetch_selected_specs,
)


class _Adapter:
    def __init__(self, spec):
        self.spec = spec

    def fetch_instrument_spec(self, _inst_id, refresh=False):
        self.refresh = refresh
        return self.spec


class NativeMetadataTests(unittest.TestCase):
    def _spec(self, venue, *, status="trading", quantity_unit="contracts", raw=None):
        return SimpleNamespace(
            venue=venue, inst_id="BTC-USDT-SWAP", base="BTC",
            tick_size=0.1, step_size=0.001, ct_val=0.01, min_size=1,
            max_leverage=20, status=status, quantity_unit=quantity_unit,
            decimal_amount=False, raw=raw or {},
        )

    def test_serializes_real_per_venue_quantity_semantics(self):
        adapters = {
            "binance": _Adapter(self._spec("binance", quantity_unit="base_asset",
                                             raw={"contractType": "PERPETUAL",
                                                  "quoteAsset": "USDT",
                                                  "marginAsset": "USDT"})),
            "gate": _Adapter(self._spec("gate", raw={"status": "trading"})),
        }
        with patch("astra_backend.instrument_metadata.get_adapter",
                   side_effect=lambda venue, environment=None: adapters[venue]), \
             patch("astra_backend.instrument_metadata.legacy_environment_for",
                   side_effect=lambda venue: "live"):
            specs = fetch_selected_specs("BTC-USDT-SWAP", ["gate", "binance"])
        self.assertEqual(specs["binance"]["quantity_unit"], "base_asset")
        self.assertFalse(specs["binance"]["decimal_amount"])
        self.assertEqual(specs["gate"]["ct_val"], 0.01)
        self.assertEqual(adapters["binance"].refresh, True)

    def test_rejects_non_trading_native_spec(self):
        adapter = _Adapter(self._spec("gate", status="delisting"))
        with patch("astra_backend.instrument_metadata.get_adapter", return_value=adapter), \
             patch("astra_backend.instrument_metadata.legacy_environment_for", return_value="live"):
            with self.assertRaisesRegex(ValueError, "GATE 合约状态不可交易"):
                fetch_selected_specs("BTC-USDT-SWAP", ["gate"])

    def test_legacy_fields_are_derived_from_real_spec(self):
        specs = {"binance": {
            "tick_size": 0.01, "ct_val": 1.0, "min_size": 0.001,
            "step_size": 0.001, "quantity_unit": "base_asset",
        }}
        item = add_native_specs({"instId": "BTC-USDT-SWAP"}, specs)
        self.assertEqual(item["metadata_venue"], "binance")
        self.assertEqual(item["minSz"], "0.001")
        self.assertEqual(item["venue_specs"], specs)
        self.assertEqual(canonical_pool_metadata(specs)["precision"], 2)


if __name__ == "__main__":
    unittest.main()
