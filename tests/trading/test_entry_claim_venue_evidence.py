"""Offline tests for normalized read-only entry-claim venue evidence."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from astra_backend.exchanges.binance import BinanceAdapter
from astra_backend.exchanges.gate import GateAdapter
from scripts.trader.entry_claim_reconciliation import decide_claim_evidence
from scripts.trader.entry_claim_venue_evidence import VenueEntryEvidenceReader


class FakeOKX:
    def __init__(self):
        self.calls = []

    def current_environment(self):
        return SimpleNamespace(mode="demo")

    def pending_orders(self, **kwargs):
        self.calls.append(("live", kwargs))
        return [{
            "ordId": "okx-order-1",
            "clOrdId": "astra0123456789abcdef0123",
            "instId": "BTC-USDT-SWAP",
            "side": "buy",
            "state": "live",
        }]

    def orders_history(self, **kwargs):
        self.calls.append(("history", kwargs))
        return []

    def fills(self, **kwargs):
        self.calls.append(("fills", kwargs))
        return []

    def positions(self, **kwargs):
        self.calls.append(("positions", kwargs))
        return []


class FakeAdapter:
    def __init__(self, *, live=None, history=None, fills=None, positions=None):
        self.live = [] if live is None else live
        self.history = [] if history is None else history
        self.fills = [] if fills is None else fills
        self.position_rows = [] if positions is None else positions
        self.calls = []

    @staticmethod
    def native_symbol(inst_id):
        return "BTCUSDT"

    def open_orders(self, symbol):
        self.calls.append(("live", symbol))
        return self.live

    def fetch_order_history(self, symbol, **kwargs):
        self.calls.append(("history", symbol, kwargs))
        return self.history

    def fetch_account_trades(self, symbol, **kwargs):
        self.calls.append(("fills", symbol, kwargs))
        return self.fills

    def positions(self):
        self.calls.append(("positions",))
        return self.position_rows


class FakeGateAdapter:
    def __init__(self, *, full_pages=False, full_first_page=False):
        self.full_pages = full_pages
        self.full_first_page = full_first_page
        self.calls = []

    def list_open_orders(self, inst_id):
        self.calls.append(("live", inst_id))
        return [{
            "id": "gate-order-1",
            "text": "t-astra-0123456789abcdef012",
            "contract": "BTC_USDT",
            "size": -1,
            "status": "open",
        }]

    def fetch_order_history_timerange(self, inst_id, **kwargs):
        self.calls.append(("history", inst_id, kwargs))
        if self.full_pages or kwargs["offset"] == 0:
            full_page = self.full_pages or self.full_first_page
            return [{
                "id": "gate-order-1",
                "text": "t-astra-0123456789abcdef012",
                "contract": "BTC_USDT",
                "size": -1,
                "status": "open",
            }] * (100 if full_page else 1)
        return []

    def fetch_personal_trades_timerange(self, inst_id, **kwargs):
        self.calls.append(("fills", inst_id, kwargs))
        if self.full_pages or (self.full_first_page and kwargs["offset"] == 0):
            return [{
                "order_id": "gate-order-1",
                "text": "t-astra-0123456789abcdef012",
                "contract": "BTC_USDT",
                "size": -1,
            }] * 100
        return []

    def positions(self):
        self.calls.append(("positions",))
        return []


def _claim(venue, **overrides):
    defaults = {
        "venue": venue,
        "environment": "demo",
        "instId": "BTC-USDT-SWAP",
        "side": "long",
        "status": "unknown_after_send",
        "claimed_at_ms": 1_000_000,
        "client_order_id": "astra0123456789abcdef0123",
    }
    defaults.update(overrides)
    return defaults


class VenueEntryEvidenceReaderTests(unittest.TestCase):
    def test_okx_normalizes_live_order_and_enforces_environment(self):
        rest = FakeOKX()
        reader = VenueEntryEvidenceReader(okx_rest=rest, now_ms=lambda: 2_000_000)
        evidence = reader.read_entry_evidence(_claim("okx"))
        decision = decide_claim_evidence(_claim("okx"), evidence)

        self.assertEqual(decision["category"], "promoted_to_active")
        self.assertEqual(decision["order_id"], "okx-order-1")
        self.assertTrue(evidence["complete"])
        self.assertEqual([call[0] for call in rest.calls], [
            "live", "history", "fills", "positions",
        ])
        self.assertEqual(rest.calls[1][1]["begin"], 1_000_000)

    def test_okx_environment_mismatch_fails_before_queries(self):
        class LiveEnvironmentOKX(FakeOKX):
            def current_environment(self):
                return SimpleNamespace(mode="live")

        rest = LiveEnvironmentOKX()
        reader = VenueEntryEvidenceReader(okx_rest=rest, now_ms=lambda: 2_000_000)
        with self.assertRaises(RuntimeError):
            reader.read_entry_evidence(_claim("okx"))
        self.assertEqual(rest.calls, [])

    def test_okx_old_claim_has_incomplete_negative_coverage(self):
        now_ms = 40 * 24 * 60 * 60 * 1000
        claim = _claim("okx", claimed_at_ms=1)
        reader = VenueEntryEvidenceReader(okx_rest=FakeOKX(), now_ms=lambda: now_ms)
        evidence = reader.read_entry_evidence(claim)
        self.assertFalse(evidence["complete"])
        self.assertFalse(evidence["coverage"]["historical_orders"])
        self.assertFalse(evidence["coverage"]["fills"])

    def test_binance_bounds_history_and_marks_old_claim_incomplete(self):
        now_ms = 10 * 24 * 60 * 60 * 1000
        adapter = FakeAdapter()
        providers = []

        def provide(venue, *, environment):
            providers.append((venue, environment))
            return adapter

        reader = VenueEntryEvidenceReader(
            adapter_provider=provide, now_ms=lambda: now_ms)
        evidence = reader.read_entry_evidence(_claim("binance", claimed_at_ms=1))

        self.assertEqual(providers, [("binance", "demo")])
        history_call = next(call for call in adapter.calls if call[0] == "history")
        self.assertEqual(history_call[2]["start_time"], now_ms - 6 * 24 * 60 * 60 * 1000)
        self.assertEqual(history_call[2]["end_time"], now_ms)
        self.assertFalse(evidence["coverage"]["historical_orders"])
        self.assertFalse(evidence["coverage"]["fills"])

    def test_binance_normalizes_correlated_history(self):
        adapter = FakeAdapter(history=[{
            "orderId": 42,
            "clientOrderId": "astra0123456789abcdef0123",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "status": "NEW",
        }])
        reader = VenueEntryEvidenceReader(
            adapter_provider=lambda *_args, **_kwargs: adapter,
            now_ms=lambda: 2_000_000)
        claim = _claim("binance")
        evidence = reader.read_entry_evidence(claim)
        decision = decide_claim_evidence(claim, evidence)
        self.assertEqual(decision["category"], "promoted_to_active")
        self.assertEqual(decision["order_id"], "42")
        self.assertTrue(evidence["complete"])

    def test_ambiguous_failed_history_does_not_become_pre_acceptance_rejection(self):
        adapter = FakeAdapter(history=[{
            "orderId": 43,
            "clientOrderId": "astra0123456789abcdef0123",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "status": "FAILED",
        }])
        reader = VenueEntryEvidenceReader(
            adapter_provider=lambda *_args, **_kwargs: adapter,
            now_ms=lambda: 2_000_000)
        claim = _claim("binance")
        evidence = reader.read_entry_evidence(claim)
        decision = decide_claim_evidence(claim, evidence)

        self.assertNotIn("accepted", evidence["historical_orders"][0])
        self.assertNotIn("pre_acceptance_terminal", evidence["historical_orders"][0])
        self.assertEqual(decision["category"], "retained_unknown")

    def test_terminal_status_obeys_explicit_acceptance_fields(self):
        claim = _claim("binance")
        rejected_after_acceptance = FakeAdapter(history=[{
            "orderId": 44,
            "clientOrderId": claim["client_order_id"],
            "symbol": "BTCUSDT",
            "side": "BUY",
            "status": "REJECTED",
            "accepted": True,
        }])
        reader = VenueEntryEvidenceReader(
            adapter_provider=lambda *_args, **_kwargs: rejected_after_acceptance,
            now_ms=lambda: 2_000_000)
        evidence = reader.read_entry_evidence(claim)
        decision = decide_claim_evidence(claim, evidence)
        self.assertFalse(evidence["historical_orders"][0].get("pre_acceptance_terminal"))
        self.assertEqual(decision["category"], "promoted_to_active")

        canceled_before_acceptance = FakeAdapter(history=[{
            "orderId": 45,
            "clientOrderId": claim["client_order_id"],
            "symbol": "BTCUSDT",
            "side": "BUY",
            "status": "CANCELED",
            "accepted": False,
        }])
        reader = VenueEntryEvidenceReader(
            adapter_provider=lambda *_args, **_kwargs: canceled_before_acceptance,
            now_ms=lambda: 2_000_000)
        evidence = reader.read_entry_evidence(claim)
        decision = decide_claim_evidence(claim, evidence)
        self.assertEqual(decision["category"], "released_with_evidence")

    def test_binance_page_cap_keeps_negative_coverage_incomplete(self):
        adapter = FakeAdapter(history=[{}] * 1000)
        reader = VenueEntryEvidenceReader(
            adapter_provider=lambda *_args, **_kwargs: adapter,
            now_ms=lambda: 2_000_000)
        evidence = reader.read_entry_evidence(_claim("binance"))
        self.assertFalse(evidence["coverage"]["historical_orders"])

    def test_gate_normalizes_client_tag_and_paginates(self):
        adapter = FakeGateAdapter(full_first_page=True)
        providers = []

        def provide(venue, *, environment):
            providers.append((venue, environment))
            return adapter

        reader = VenueEntryEvidenceReader(
            adapter_provider=provide, now_ms=lambda: 2_000_000)
        claim = _claim(
            "gate", side="short", client_order_id="t-astra-0123456789abcdef012")
        evidence = reader.read_entry_evidence(claim)
        decision = decide_claim_evidence(claim, evidence)

        self.assertEqual(providers, [("gate", "demo")])
        self.assertEqual(decision["category"], "promoted_to_active")
        self.assertEqual(decision["order_id"], "gate-order-1")
        history_calls = [call for call in adapter.calls if call[0] == "history"]
        self.assertEqual([call[2]["offset"] for call in history_calls], [0, 100])
        self.assertTrue(evidence["complete"])

    def test_gate_adapter_environment_mismatch_fails_before_queries(self):
        class WrongEnvironmentAdapter(FakeAdapter):
            environment = "live"

        adapter = WrongEnvironmentAdapter()
        reader = VenueEntryEvidenceReader(
            adapter_provider=lambda *_args, **_kwargs: adapter,
            now_ms=lambda: 2_000_000)
        with self.assertRaisesRegex(RuntimeError, "environment mismatch"):
            reader.read_entry_evidence(_claim(
                "gate", client_order_id="t-astra-0123456789abcdef012"))
        self.assertEqual(adapter.calls, [])

    def test_gate_page_cap_keeps_negative_coverage_incomplete(self):
        adapter = FakeGateAdapter(full_pages=True)
        reader = VenueEntryEvidenceReader(
            adapter_provider=lambda *_args, **_kwargs: adapter,
            now_ms=lambda: 2_000_000)
        evidence = reader.read_entry_evidence(_claim(
            "gate", client_order_id="t-astra-0123456789abcdef012"))
        history_calls = [call for call in adapter.calls if call[0] == "history"]
        self.assertEqual(len(history_calls), 20)
        self.assertEqual(history_calls[-1][2]["offset"], 1900)
        self.assertFalse(evidence["coverage"]["historical_orders"])
        self.assertFalse(evidence["coverage"]["fills"])
        self.assertFalse(evidence["complete"])

    def test_binance_read_only_wrappers_use_expected_sdk_operations(self):
        adapter = BinanceAdapter.__new__(BinanceAdapter)
        calls = []
        adapter.native_symbol = lambda _symbol: "BTCUSDT"
        adapter._private_call = lambda method, **kwargs: (
            calls.append((method, kwargs)) or [])

        adapter.fetch_order_history(
            "BTC-USDT-SWAP", start_time=100, end_time=200, limit=1000)
        adapter.fetch_account_trades(
            "BTC-USDT-SWAP", start_time=100, end_time=200, limit=1000)

        self.assertEqual(calls, [
            ("all_orders", {
                "symbol": "BTCUSDT", "start_time": 100,
                "end_time": 200, "limit": 1000,
            }),
            ("account_trade_list", {
                "symbol": "BTCUSDT", "order_id": None,
                "start_time": 100, "end_time": 200,
                "from_id": None, "limit": 1000,
            }),
        ])

    def test_gate_read_only_wrappers_use_timerange_endpoints_and_params(self):
        adapter = GateAdapter.__new__(GateAdapter)
        calls = []
        adapter.native_symbol = lambda _symbol: "BTC_USDT"
        adapter.signed_request = lambda method, path, params=None, **_kwargs: (
            calls.append((method, path, params)) or [])

        adapter.fetch_order_history_timerange(
            "BTC-USDT-SWAP", from_s=100, to_s=200, limit=100, offset=300)
        adapter.fetch_personal_trades_timerange(
            "BTC-USDT-SWAP", from_s=100, to_s=200, limit=100, offset=300)

        expected_params = {
            "contract": "BTC_USDT", "from": 100, "to": 200,
            "limit": 100, "offset": 300,
        }
        self.assertEqual(calls, [
            ("GET", "/api/v4/futures/usdt/orders_timerange", expected_params),
            ("GET", "/api/v4/futures/usdt/my_trades_timerange", expected_params),
        ])

    def test_reader_query_exception_is_not_suppressed(self):
        class FailingAdapter(FakeAdapter):
            def open_orders(self, _symbol):
                raise TimeoutError("offline timeout")

        reader = VenueEntryEvidenceReader(
            adapter_provider=lambda *_args, **_kwargs: FailingAdapter(),
            now_ms=lambda: 2_000_000)
        with self.assertRaisesRegex(TimeoutError, "offline timeout"):
            reader.read_entry_evidence(_claim("binance"))


if __name__ == "__main__":
    unittest.main()
