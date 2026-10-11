"""Offline tests for evidence-driven entry-claim reconciliation."""
from __future__ import annotations

import unittest

from scripts.trader.entry_claim_reconciliation import (
    decide_claim_evidence,
    reconcile_claim_snapshot,
    reconcile_open_entry_claims,
)


_COMPLETE = {
    "query_ok": True,
    "venue": "binance",
    "environment": "demo",
    "complete": True,
    "coverage": {
        "live_orders": True,
        "historical_orders": True,
        "fills": True,
        "positions": True,
    },
    "live_orders": [],
    "historical_orders": [],
    "fills": [],
    "positions": [],
}


def _claim(**overrides):
    return {
        "claim_id": "claim-1",
        "environment": "demo",
        "venue": "binance",
        "instId": "BTC-USDT-SWAP",
        "side": "long",
        "status": "unknown_after_send",
        "client_order_id": "astra0123456789abcdef0123",
        **overrides,
    }


def _order(**overrides):
    return {
        "venue": "binance",
        "order_id": "order-1",
        "client_order_id": "astra0123456789abcdef0123",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "status": "NEW",
        **overrides,
    }


class EntryClaimReconciliationTests(unittest.TestCase):
    def test_unique_live_order_promotes_claim(self):
        evidence = {**_COMPLETE, "live_orders": [_order()]}
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "promoted_to_active")
        self.assertEqual(result["action"], "promote_to_active")
        self.assertEqual(result["order_id"], "order-1")

    def test_unique_historical_filled_order_promotes_claim(self):
        evidence = {
            **_COMPLETE,
            "historical_orders": [_order(status="FILLED", executedQty="0.01")],
            "fills": [{
                "venue": "binance",
                "order_id": "order-1",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "qty": "0.01",
            }],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "promoted_to_active")
        self.assertEqual(result["order_id"], "order-1")

    def test_attributable_fill_quantity_prevents_rejection_release(self):
        evidence = {
            **_COMPLETE,
            "historical_orders": [_order(status="REJECTED")],
            "fills": [{
                "venue": "binance",
                "order_id": "order-1",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "qty": "0.01",
            }],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "promoted_to_active")

    def test_client_id_fill_can_promote_without_order_history_row(self):
        evidence = {
            **_COMPLETE,
            "fills": [{
                "venue": "binance",
                "order_id": "fill-order-1",
                "client_order_id": "astra0123456789abcdef0123",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "qty": "0.01",
            }],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "promoted_to_active")
        self.assertEqual(result["order_id"], "fill-order-1")

    def test_explicit_rejection_with_complete_negative_coverage_releases(self):
        evidence = {
            **_COMPLETE,
            "historical_orders": [_order(status="REJECTED")],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "released_with_evidence")
        self.assertEqual(result["action"], "release")

    def test_complete_flag_without_four_axis_coverage_cannot_release(self):
        evidence = {
            "query_ok": True,
            "venue": "binance",
            "environment": "demo",
            "complete": True,
            "historical_orders": [_order(status="REJECTED")],
            "live_orders": [],
            "fills": [],
            "positions": [],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "retained_unknown")
        self.assertIn("coverage is incomplete", result["reason"])

    def test_canceled_order_without_pre_acceptance_evidence_is_retained(self):
        evidence = {
            **_COMPLETE,
            "historical_orders": [_order(status="CANCELED")],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "retained_unknown")
        self.assertEqual(result["action"], "retain")

    def test_canceled_pre_acceptance_order_releases_with_full_coverage(self):
        evidence = {
            **_COMPLETE,
            "historical_orders": [_order(status="CANCELED", pre_acceptance_terminal=True)],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "released_with_evidence")

    def test_canceled_order_with_incomplete_history_is_retained(self):
        evidence = {
            **_COMPLETE,
            "complete": False,
            "coverage": {
                "live_orders": True,
                "historical_orders": False,
                "fills": True,
                "positions": True,
            },
            "historical_orders": [_order(status="CANCELED", accepted=False)],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "retained_unknown")

    def test_query_timeout_retains_claim(self):
        evidence = {"query_ok": False, "error": "timeout"}
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "query_failures")
        self.assertEqual(result["action"], "retain")

    def test_multiple_matching_orders_are_ambiguous(self):
        evidence = {
            **_COMPLETE,
            "historical_orders": [
                _order(order_id="order-1", status="NEW"),
                _order(order_id="order-2", status="NEW"),
            ],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "ambiguous")

    def test_position_only_evidence_does_not_promote_or_release(self):
        evidence = {
            **_COMPLETE,
            "positions": [{"symbol": "BTCUSDT", "positionAmt": "0.01"}],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "retained_unknown")
        self.assertIn("not attributable", result["reason"])

    def test_manual_order_with_different_client_id_is_not_attributed(self):
        evidence = {
            **_COMPLETE,
            "live_orders": [_order(client_order_id="manual-user-order")],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "retained_unknown")

    def test_evidence_scope_and_order_venue_must_match_claim(self):
        scoped = {**_COMPLETE, "live_orders": [_order()]}
        wrong_venue = decide_claim_evidence(
            _claim(), {**scoped, "venue": "gate"})
        wrong_environment = decide_claim_evidence(
            _claim(), {**scoped, "environment": "live"})
        wrong_row_venue = decide_claim_evidence(
            _claim(), {**scoped, "live_orders": [_order(venue="gate")]})

        self.assertEqual(wrong_venue["category"], "retained_unknown")
        self.assertEqual(wrong_environment["category"], "retained_unknown")
        self.assertEqual(wrong_row_venue["category"], "retained_unknown")

    def test_empty_live_and_history_is_not_proof_of_rejection(self):
        result = decide_claim_evidence(_claim(), _COMPLETE)
        self.assertEqual(result["category"], "retained_unknown")

    def test_correlation_match_requires_exact_instrument_and_direction(self):
        evidence = {
            **_COMPLETE,
            "live_orders": [_order(symbol="ETHUSDT")],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "retained_unknown")

    def test_position_in_opposite_direction_does_not_count_as_target_exposure(self):
        evidence = {
            **_COMPLETE,
            "historical_orders": [_order(status="REJECTED")],
            "positions": [{"symbol": "BTCUSDT", "positionAmt": "-0.01"}],
        }
        result = decide_claim_evidence(_claim(), evidence)
        self.assertEqual(result["category"], "released_with_evidence")

    def test_missing_correlation_id_fails_closed(self):
        result = decide_claim_evidence(_claim(client_order_id=""), _COMPLETE)
        self.assertEqual(result["category"], "retained_unknown")
        self.assertIn("no persisted client", result["reason"])

    def test_reader_failure_is_reported_and_persist_callback_receives_audit(self):
        persisted = []
        result = reconcile_claim_snapshot(
            [_claim()],
            evidence_reader=lambda _claim: (_ for _ in ()).throw(TimeoutError("read timeout")),
            persist=lambda claim, audit: persisted.append((claim, audit)),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["counts"], {"query_failures": 1})
        self.assertEqual(persisted[0][1]["category"], "query_failures")
        self.assertNotIn("raw_evidence", persisted[0][1])

    def test_durable_worker_promotes_unknown_and_preserves_audit(self):
        import json
        import tempfile
        from pathlib import Path
        from scripts.trader.open_entry_guard import (
            claim_open_entry, mark_open_entry_submitting, mark_open_entry_unknown,
        )

        with tempfile.TemporaryDirectory(prefix="astra-reconcile-worker-") as tmp:
            path = Path(tmp) / "claims.json"
            claimed = claim_open_entry(
                environment="demo", venue="binance", inst_id="BTC-USDT-SWAP",
                side="long", path=path, now_ms=1000)
            self.assertTrue(claimed[0], claimed)
            self.assertTrue(mark_open_entry_submitting(claimed[2], path=path, now_ms=1001))
            self.assertTrue(mark_open_entry_unknown(claimed[2], "timeout", path=path, now_ms=1002))
            before = json.loads(path.read_text(encoding="utf-8"))
            calls = []

            def read_only_reader(claim):
                calls.append(claim["client_order_id"])
                return {**_COMPLETE, "live_orders": [_order(
                    client_order_id=claim["client_order_id"])]}

            result = reconcile_open_entry_claims(
                evidence_reader=read_only_reader, path=str(path), now_ms=2000)
            after = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"], result)
            self.assertEqual(calls, [before[0]["client_order_id"]])
            self.assertEqual(result["counts"], {"promoted_to_active": 1})
            self.assertEqual(after[0]["status"], "active")
            self.assertEqual(after[0]["order_id"], "order-1")
            self.assertEqual(after[0]["last_reconciliation"]["category"],
                             "promoted_to_active")

    def test_durable_worker_releases_only_with_complete_rejection_evidence(self):
        import json
        import tempfile
        from pathlib import Path
        from scripts.trader.open_entry_guard import (
            claim_open_entry, mark_open_entry_submitting, mark_open_entry_unknown,
        )

        with tempfile.TemporaryDirectory(prefix="astra-reconcile-release-") as tmp:
            path = Path(tmp) / "claims.json"
            claimed = claim_open_entry(
                environment="demo", venue="gate", inst_id="BTC-USDT-SWAP",
                side="short", path=path, now_ms=1000)
            self.assertTrue(mark_open_entry_submitting(claimed[2], path=path, now_ms=1001))
            self.assertTrue(mark_open_entry_unknown(claimed[2], "timeout", path=path, now_ms=1002))

            def read_only_reader(claim):
                return {
                    **_COMPLETE,
                    "venue": "gate",
                    "historical_orders": [_order(
                        venue="gate",
                        client_order_id=claim["client_order_id"],
                        symbol="BTC_USDT", side="SELL", status="REJECTED")],
                }

            result = reconcile_open_entry_claims(
                evidence_reader=read_only_reader, path=str(path), now_ms=3000)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["counts"], {"released_with_evidence": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])
            audit_path = Path(str(path) + ".reconciliation.jsonl")
            audit = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(audit["claim_id"], claimed[2])
            self.assertEqual(audit["category"], "released_with_evidence")

    def test_non_unknown_claims_are_not_sent_to_evidence_reader(self):
        def fail_if_called(_claim):
            raise AssertionError("active claims are not reconciliation candidates")

        result = reconcile_claim_snapshot(
            [_claim(status="active")], evidence_reader=fail_if_called)
        self.assertEqual(result["reports"], [])
        self.assertEqual(result["counts"], {})


if __name__ == "__main__":
    unittest.main()
