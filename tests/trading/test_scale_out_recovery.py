"""Persistent operation and fill-reconciliation contracts for scale-out."""
import unittest
from unittest.mock import patch

from scripts.trader import scale_out as so


class ScaleOutRecoveryContracts(unittest.TestCase):
    def test_order_outcomes_are_distinct(self):
        self.assertEqual(
            so.classify_scale_out_order_result({"ordId": "srv-1"})[0],
            so.SCALE_OUT_ACCEPTED_WITH_ID,
        )
        self.assertEqual(
            so.classify_scale_out_order_result({"code": "51000", "msg": "rejected"})[0],
            so.SCALE_OUT_EXPLICIT_REJECTION,
        )
        self.assertEqual(
            so.classify_scale_out_order_result(error=TimeoutError("read timed out"))[0],
            so.SCALE_OUT_UNKNOWN,
        )
        self.assertEqual(
            so.classify_scale_out_order_result({"code": "0", "data": []})[0],
            so.SCALE_OUT_UNKNOWN,
        )

    def test_delta_reconcile_requires_observed_reduction(self):
        self.assertEqual(
            so.reconcile_scale_out_position_delta(10, 5, 5)[0], so.SCALE_OUT_FILLED)
        self.assertEqual(
            so.reconcile_scale_out_position_delta(10, 8, 5)[0], so.SCALE_OUT_PARTIAL)
        self.assertEqual(
            so.reconcile_scale_out_position_delta(10, 10, 5)[0], so.SCALE_OUT_NOT_FILLED)
        self.assertEqual(
            so.reconcile_scale_out_position_delta(10, None, 5)[0], so.SCALE_OUT_UNKNOWN)

    def test_unknown_submit_keeps_durable_operation_claim(self):
        f = {"instId": "BTC-USDT-SWAP", "name": "BTC", "price": 100.0,
             "atr": 2.0, "precision": 2, "ctVal": 1.0, "minSz": 0.1,
             "market_data_valid": True}
        pos = {"pos": "3.4", "side": "long", "avgPx": 90.0, "venue": "binance"}
        key = "BTC-USDT-SWAP_long"
        trackers = {key: {"scale_out_phase": 0, "takeProfitPx": 0.0}}
        actions = []

        class Adapter:
            def list_protective_orders(self, symbol=None):
                return []
            def place_order(self, *args, **kwargs):
                raise TimeoutError("request timed out")

        class Registry:
            def get_adapter(self, venue, environment=None):
                return Adapter()

        with patch.object(so, "SCALE_OUT_RATIO", 0.5):
            ok, reason = so.execute_scale_out_if_eligible(
                f, pos, trackers, "2026-10-10 12:00:00", actions,
                venue_registry=Registry(), save_trackers=lambda value: None,
            )
        self.assertFalse(ok)
        self.assertIn("未知", reason)
        operation = trackers[key].get("scale_out_operation")
        self.assertIsInstance(operation, dict)
        self.assertEqual(operation.get("state"), "unknown")

    def test_restart_reconciles_fill_before_replacing_protection_without_second_close(self):
        import copy

        f = {"instId": "BTC-USDT-SWAP", "name": "BTC", "price": 110.0,
             "atr": 2.0, "precision": 2, "ctVal": 1.0, "minSz": 0.1,
             "market_data_valid": True}
        pos = {"pos": "10", "side": "long", "avgPx": 100.0,
               "venue": "okx", "ctVal": 1.0, "minSz": 0.1, "precision": 2}
        key = "BTC-USDT-SWAP_long"
        trackers = {key: {"scale_out_phase": 0, "takeProfitPx": 120.0}}
        persisted = []
        actions = []

        class Adapter:
            def __init__(self):
                self.rows = [
                    {"algoId": "old-1", "algoClOrdId": "t-astrasl-old-1",
                     "instId": "BTC-USDT-SWAP", "symbol": "BTC-USDT-SWAP",
                     "posSide": "long", "side": "sell", "sz": "10",
                     "ordType": "conditional", "reduceOnly": True,
                     "slTriggerPx": "90", "state": "live"},
                    {"algoId": "manual-1", "instId": "BTC-USDT-SWAP",
                     "symbol": "BTC-USDT-SWAP", "posSide": "long",
                     "side": "sell", "sz": "10", "ordType": "conditional",
                     "reduceOnly": True, "slTriggerPx": "85", "state": "live"},
                ]
                self.close_submits = []
                self.cancel_calls = []

            def pending_algo_orders(self, inst_id):
                return [dict(row) for row in self.rows]

            def place_order(self, *args, **kwargs):
                self.close_submits.append((args, kwargs))
                return [{"ordId": "close-1"}]

            def ensure_protection(self, inst_id, pos_side, size, tp_px, sl_px):
                self.rows.append({
                    "algoId": "new-1", "algoClOrdId": "t-astraProtect-new-1",
                    "instId": inst_id, "symbol": inst_id, "posSide": pos_side,
                    "side": "sell", "sz": str(size), "ordType": "oco",
                    "reduceOnly": True, "slTriggerPx": str(sl_px),
                    "tpTriggerPx": str(tp_px), "state": "live",
                })
                return True, "ok"

            def cancel_algo_orders(self, ids, *, inst_id):
                self.cancel_calls.append((copy.deepcopy(ids), inst_id))
                wanted = {
                    str(row.get("algoId") or row.get("algoClOrdId") or "")
                    for row in ids if isinstance(row, dict)
                }
                self.rows = [row for row in self.rows
                             if str(row.get("algoId") or "") not in wanted
                             and str(row.get("algoClOrdId") or "") not in wanted]
                return [{"algoId": next(iter(wanted), ""), "sCode": "0"}]

        adapter = Adapter()
        query_calls = []

        def query_position(inst_id, pos_side, venue):
            query_calls.append((inst_id, pos_side, venue))
            if len(query_calls) == 1:
                raise TimeoutError("position query timed out after accepted close")
            return {"pos": "5"}

        save_trackers = lambda value: persisted.append(copy.deepcopy(value))
        with patch.object(so, "SCALE_OUT_RATIO", 0.5):
            ok, reason = so.execute_scale_out_if_eligible(
                f, pos, trackers, "2026-10-10 12:00:00", actions,
                okx_rest=adapter, query_position=query_position,
                save_trackers=save_trackers,
            )
        self.assertFalse(ok)
        self.assertIn("等待实际减仓核验", reason)
        self.assertEqual(len(adapter.close_submits), 1)
        durable_trackers = copy.deepcopy(persisted[-1])
        operation = durable_trackers[key].get("scale_out_operation")
        self.assertIsInstance(operation, dict)
        self.assertEqual(operation.get("state"), "unknown")
        self.assertEqual(operation.get("old_protection_identities")[0]["identity_value"], "old-1")

        # A new process starts from the last durable tracker snapshot while its
        # caller still has the pre-close position snapshot (10). Recovery must
        # use the exchange-observed post-close size (5), not submit another close.
        restarted_trackers = copy.deepcopy(durable_trackers)
        with patch.object(so, "SCALE_OUT_RATIO", 0.5):
            ok, reason = so.recover_scale_out_operations(
                f, pos, restarted_trackers, "2026-10-10 12:15:00", actions,
                okx_rest=adapter, query_position=query_position,
                ensure_cloud_position_protection=adapter.ensure_protection,
                save_trackers=save_trackers,
            )
        self.assertTrue(ok, reason)
        self.assertEqual(len(adapter.close_submits), 1)
        self.assertEqual(len(query_calls), 2)
        self.assertEqual(restarted_trackers[key]["currentSz"], 5.0)
        self.assertEqual(restarted_trackers[key]["scale_out_phase"], 1)
        self.assertNotIn("scale_out_operation", restarted_trackers[key])
        self.assertEqual(len(adapter.cancel_calls), 1)
        self.assertEqual(adapter.cancel_calls[0][0][0]["algoId"], "old-1")
        live_ids = {row.get("algoId") for row in adapter.rows}
        self.assertNotIn("old-1", live_ids)
        self.assertIn("new-1", live_ids)
        self.assertIn("manual-1", live_ids)


class TypedProtectionRecoveryTests(unittest.TestCase):
    @staticmethod
    def _tagged_row(**ids):
        return {
            **ids,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "STOP_MARKET",
            "quantity": "5",
            "reduceOnly": True,
            "triggerPrice": "90.225",
            "clientOrderId": "t-astrasl-offline-fixture",
        }

    @staticmethod
    def _old_algo_identity(value="42"):
        return {
            "venue": "binance", "resource_family": "algo_order",
            "identity_kind": "algo_id", "identity_value": value,
        }

    def test_legacy_token_upgrades_only_one_live_tagged_typed_identity(self):
        row = self._tagged_row(algoId="42", clientAlgoId="t-astrasl-old")
        identities, unresolved, detail = so._resolve_legacy_protection_tokens(
            ["42"], [row], venue="binance", inst_id="BTC-USDT-SWAP", pos_side="long")
        self.assertEqual(unresolved, [])
        self.assertEqual(detail, "")
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["identity_kind"], "algo_id")
        self.assertEqual(identities[0]["identity_value"], "42")

    def test_legacy_token_collision_across_namespaces_remains_unresolved(self):
        rows = [
            self._tagged_row(algoId="42", clientAlgoId="t-astrasl-algo"),
            self._tagged_row(clientAlgoId="42"),
        ]
        identities, unresolved, detail = so._resolve_legacy_protection_tokens(
            ["42"], rows, venue="binance", inst_id="BTC-USDT-SWAP", pos_side="long")
        self.assertEqual(identities, [])
        self.assertEqual(unresolved, ["42"])
        self.assertIn("未能唯一升级", detail)

    def test_zero_legacy_matches_are_not_guessed(self):
        identities, unresolved, _detail = so._resolve_legacy_protection_tokens(
            ["missing"], [self._tagged_row(algoId="42")], venue="binance",
            inst_id="BTC-USDT-SWAP", pos_side="long")
        self.assertEqual(identities, [])
        self.assertEqual(unresolved, ["missing"])

    def test_replacement_reusing_old_typed_key_is_rejected_even_with_new_alias(self):
        row = self._tagged_row(algoId="42", clientAlgoId="t-astrasl-new")
        ok, detail, identities = so._verify_replacement_protection(
            [row], venue="binance", inst_id="BTC-USDT-SWAP", pos_side="long",
            remaining_size=5.0, sl_px=90.225, tp_px=None,
            old_identities=[self._old_algo_identity("42")])
        self.assertFalse(ok)
        self.assertIn("复用了旧 typed identity", detail)
        self.assertEqual(identities, [])

    def test_equal_text_in_distinct_identity_namespaces_is_not_a_collision(self):
        row = self._tagged_row(clientAlgoId="42")
        ok, detail, identities = so._verify_replacement_protection(
            [row], venue="binance", inst_id="BTC-USDT-SWAP", pos_side="long",
            remaining_size=5.0, sl_px=90.225, tp_px=None,
            old_identities=[self._old_algo_identity("42")])
        self.assertTrue(ok, detail)
        self.assertEqual(len(identities), 1)
        self.assertEqual(identities[0]["identity_kind"], "client_algo_id")

    def test_string_false_reduce_only_does_not_pass_replacement_validation(self):
        row = self._tagged_row(algoId="new-42")
        row["reduceOnly"] = "false"
        ok, detail, _identities = so._verify_replacement_protection(
            [row], venue="binance", inst_id="BTC-USDT-SWAP", pos_side="long",
            remaining_size=5.0, sl_px=90.225, tp_px=None, old_identities=[])
        self.assertFalse(ok)
        self.assertIn("reduce-only/full-close", detail)


if __name__ == "__main__":
    unittest.main()
