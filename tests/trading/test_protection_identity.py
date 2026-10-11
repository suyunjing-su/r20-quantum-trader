import unittest

from scripts.trader.protection_identity import (
    collect_protection_identities,
    extract_protection_identities,
    normalize_typed_identity,
    protection_identity_key,
)
from scripts.trader.venue_protection import cancel_protective_leg


class ProtectionIdentityTest(unittest.TestCase):
    def test_binance_numeric_and_client_ids_do_not_collapse(self):
        identities = extract_protection_identities({
            "raw": {"algoId": 77, "clientAlgoId": "77", "symbol": "BTCUSDT"},
        }, venue="binance", ownership_evidence="tag", protection_kind="sl")
        self.assertEqual(
            {protection_identity_key(item) for item in identities},
            {
                ("binance", "algo_order", "algo_id", "77"),
                ("binance", "algo_order", "client_algo_id", "77"),
            },
        )
        self.assertNotEqual(protection_identity_key(identities[0]),
                            protection_identity_key(identities[1]))

    def test_gate_prefers_string_safe_identity_without_losing_id_namespace(self):
        identities = extract_protection_identities({
            "id": 9007199254740992,
            "id_string": "9007199254740993",
            "initial": {"contract": "BTC_USDT", "text": "t-astrasl-x"},
        }, venue="gate", ownership_evidence="tag", protection_kind="sl")
        self.assertEqual(
            {protection_identity_key(item) for item in identities},
            {
                ("gate", "price_order", "id", "9007199254740992"),
                ("gate", "price_order", "id_string", "9007199254740993"),
            },
        )
        self.assertTrue(all(item["symbol"] == "BTC_USDT" for item in identities))

    def test_okx_algo_id_and_client_order_id_are_distinct(self):
        identities = extract_protection_identities({
            "algoId": "42", "algoClOrdId": "42", "instId": "BTC-USDT-SWAP",
        }, venue="okx", ownership_evidence="tag", protection_kind="sl")
        self.assertEqual(
            {protection_identity_key(item) for item in identities},
            {
                ("okx", "algo_order", "algo_id", "42"),
                ("okx", "algo_order", "algo_cl_ord_id", "42"),
            },
        )

    def test_rejects_malformed_and_lossy_gate_identities(self):
        self.assertIsNone(normalize_typed_identity({
            "venue": "gate", "resource_family": "price_order",
            "identity_kind": "algo_id", "identity_value": "12",
        }))
        self.assertEqual(extract_protection_identities({"id": 1.234}, venue="gate"), [])

    def test_collection_deduplicates_only_exact_typed_keys(self):
        identities = collect_protection_identities([
            {"raw": {"algoId": "7", "clientAlgoId": "7"}},
            {"raw": {"algoId": "7", "clientAlgoId": "7"}},
        ], venue="binance")
        self.assertEqual(len(identities), 2)
        self.assertEqual(len({protection_identity_key(x) for x in identities}), 2)

    def test_gate_typed_cancel_uses_price_order_resource_only(self):
        class Adapter:
            def __init__(self):
                self.calls = []
            def cancel_price_order(self, value):
                self.calls.append(("price", value))
                return {"status": "finished"}
            def cancel_order(self, *args, **kwargs):
                self.calls.append(("ordinary", args, kwargs))
                raise AssertionError("ordinary order endpoint must not cancel a price order")

        ad = Adapter()
        identity = {"venue": "gate", "resource_family": "price_order",
                    "identity_kind": "id_string", "identity_value": "9007199254740993"}
        self.assertTrue(cancel_protective_leg(
            ad, "9007199254740993", identity=identity, venue="gate"))
        self.assertEqual(ad.calls, [("price", "9007199254740993")])

    def test_binance_typed_cancel_uses_algo_namespace_only(self):
        class Adapter:
            def __init__(self):
                self.calls = []
            def cancel_algo_order(self, **kwargs):
                self.calls.append(("algo", kwargs))
                return {"code": "200", "msg": "success"}
            def cancel_order(self, *args, **kwargs):
                self.calls.append(("ordinary", args, kwargs))
                raise AssertionError("ordinary order endpoint must not cancel an algo order")

        ad = Adapter()
        identity = {"venue": "binance", "resource_family": "algo_order",
                    "identity_kind": "client_algo_id", "identity_value": "42"}
        self.assertTrue(cancel_protective_leg(ad, "42", identity=identity, venue="binance"))
        self.assertEqual(ad.calls, [("algo", {"client_algo_id": "42"})])


if __name__ == "__main__":
    unittest.main()
