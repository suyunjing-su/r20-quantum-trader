import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.trader.execution_evidence import (
    directional_slippage_bps,
    load_binance_execution_evidence,
    measure_fill_vwap,
    persist_binance_execution_evidence,
)


class ExecutionEvidenceTests(unittest.TestCase):
    def test_fill_vwap_uses_actual_fill_quantity_and_price(self):
        result = measure_fill_vwap([
            {"qty": "2", "price": "100"},
            {"qty": "1", "price": "103"},
            {"qty": "0", "price": "99"},
        ])
        self.assertEqual(result["status"], "OBSERVED")
        self.assertEqual(result["filled_qty"], 3)
        self.assertAlmostEqual(result["vwap"], 101)

    def test_missing_fill_or_reference_stays_unobserved(self):
        self.assertEqual(measure_fill_vwap([]), {
            "filled_qty": None, "vwap": None, "status": "UNOBSERVED"})
        self.assertIsNone(directional_slippage_bps("BUY", None, 100))
        self.assertAlmostEqual(directional_slippage_bps("BUY", 100, 101), 100)
        self.assertAlmostEqual(directional_slippage_bps("SELL", 100, 99), 100)

    def test_evidence_sidecar_persists_by_exchange_order_id(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"ASTRA_DATA_DIR": tmp}):
            evidence = {"entry_order_id": "123", "spread_bps": None,
                        "book_status": "UNOBSERVED"}
            self.assertTrue(persist_binance_execution_evidence(evidence))
            close_evidence = {"order_id": "456", "close_reference_price": 99.5}
            self.assertTrue(persist_binance_execution_evidence(close_evidence))
            path = str(Path(tmp) / "binance_execution_evidence.json")
            self.assertEqual(load_binance_execution_evidence(path), {
                "123": evidence, "456": close_evidence})
            with open(path, encoding="utf-8") as handle:
                self.assertIsNone(json.load(handle)["123"]["spread_bps"])


if __name__ == "__main__":
    unittest.main()
