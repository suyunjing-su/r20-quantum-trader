"""订单提交前持久化防重闸的离线测试。"""
from __future__ import annotations

import json
import threading
import tempfile
import unittest
from pathlib import Path

from scripts.trader.open_entry_guard import (
    claim_open_entry,
    begin_open_entry_submission,
    commit_open_entry,
    mark_open_entry_submitting,
    mark_open_entry_unknown,
    release_open_entries_for,
    release_open_entry,
    classify_entry_submission_exception,
    validate_open_entry_claim,
)


class OpenEntryGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="astra-entry-guard-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "claims.json"

    def test_same_direction_is_blocked_across_route_changes(self):
        first = claim_open_entry(environment="demo", venue="okx",
                                 inst_id="ZEN-USDT-SWAP", side="short",
                                 path=self.path, now_ms=1000)
        self.assertTrue(first[0], first)
        self.assertTrue(commit_open_entry(first[2], "o-1", path=self.path))
        second = claim_open_entry(environment="demo", venue="gate",
                                  inst_id="ZEN-USDT-SWAP", side="short",
                                  path=self.path, now_ms=2000)
        self.assertFalse(second[0])
        self.assertIn("同标的同方向", second[1])

    def test_failed_submit_can_release_but_unknown_claim_is_retained(self):
        claim = claim_open_entry(environment="demo", venue="okx",
                                 inst_id="BTC-USDT-SWAP", side="long",
                                 path=self.path, now_ms=1000)
        self.assertTrue(claim[0])
        self.assertFalse(release_open_entry(claim[2], path=self.path))
        self.assertTrue(release_open_entry(
            claim[2], confirmed_not_sent=True, path=self.path))
        retry = claim_open_entry(environment="demo", venue="okx",
                                 inst_id="BTC-USDT-SWAP", side="long",
                                 path=self.path, now_ms=2000)
        self.assertTrue(retry[0], retry)
        self.assertTrue(commit_open_entry(retry[2], "o-2", path=self.path))
        self.assertEqual(release_open_entries_for(environment="demo",
                                                   inst_id="BTC-USDT-SWAP",
                                                   side="long", path=self.path), 1)

    def test_uncertain_submit_claim_never_expires_without_reconciliation(self):
        claimed = claim_open_entry(environment="demo", venue="okx",
                                   inst_id="BTC-USDT-SWAP", side="long",
                                   path=self.path, now_ms=1_000, ttl_seconds=1)
        self.assertTrue(claimed[0], claimed)
        self.assertTrue(mark_open_entry_submitting(
            claimed[2], path=self.path, now_ms=1_001))
        self.assertTrue(mark_open_entry_unknown(
            claimed[2], "read timeout", path=self.path, now_ms=1_002))

        duplicate = claim_open_entry(environment="demo", venue="gate",
                                     inst_id="BTC_USDT", side="long",
                                     path=self.path, now_ms=24 * 60 * 60 * 1000)
        self.assertFalse(duplicate[0], duplicate)
        self.assertFalse(validate_open_entry_claim(
            claimed[2], environment="demo", inst_id="BTC-USDT-SWAP",
            side="long", path=self.path, now_ms=24 * 60 * 60 * 1000))

        # Only a later exchange reconciliation can resolve the uncertain claim.
        self.assertTrue(commit_open_entry(
            claimed[2], "server-order-7", path=self.path, now_ms=25 * 60 * 60 * 1000))
        retry = claim_open_entry(environment="demo", venue="gate",
                                 inst_id="BTC_USDT", side="long",
                                 path=self.path, now_ms=25 * 60 * 60 * 1000 + 1)
        self.assertFalse(retry[0], retry)

    def test_submitting_claim_cannot_be_replayed_as_a_second_submit(self):
        claimed = claim_open_entry(environment="demo", venue="okx",
                                   inst_id="ETH-USDT-SWAP", side="short",
                                   path=self.path, now_ms=1_000, ttl_seconds=1)
        self.assertTrue(claimed[0], claimed)
        self.assertTrue(mark_open_entry_submitting(
            claimed[2], path=self.path, now_ms=1_001))
        self.assertTrue(validate_open_entry_claim(
            claimed[2], environment="demo", inst_id="ETH-USDT-SWAP",
            side="short", path=self.path, now_ms=90_000_000))
        self.assertTrue(begin_open_entry_submission(
            claimed[2], path=self.path, now_ms=1_002))
        self.assertFalse(validate_open_entry_claim(
            claimed[2], environment="demo", inst_id="ETH-USDT-SWAP",
            side="short", path=self.path, now_ms=1_003))
        self.assertFalse(begin_open_entry_submission(
            claimed[2], path=self.path, now_ms=1_003))

    def test_confirmed_close_does_not_release_unknown_submission(self):
        claimed = claim_open_entry(environment="demo", venue="okx",
                                   inst_id="SOL-USDT-SWAP", side="short",
                                   path=self.path, now_ms=1_000)
        self.assertTrue(claimed[0], claimed)
        self.assertTrue(mark_open_entry_submitting(
            claimed[2], path=self.path, now_ms=1_001))
        self.assertTrue(mark_open_entry_unknown(
            claimed[2], "transport timeout", path=self.path, now_ms=1_002))
        self.assertEqual(release_open_entries_for(
            environment="demo", inst_id="SOL_USDT", side="short", path=self.path), 0)
        blocked = claim_open_entry(environment="demo", venue="gate",
                                   inst_id="SOL-USDT-SWAP", side="short",
                                   path=self.path, now_ms=20_000_000)
        self.assertFalse(blocked[0], blocked)

    def test_concurrent_claims_allow_only_one_submitter(self):
        results = []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            results.append(claim_open_entry(
                environment="live", venue="okx", inst_id="ZEN-USDT-SWAP",
                side="short", path=self.path, now_ms=10_000))

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for item in results if item[0]), 1, results)
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))), 1)

    def test_supplied_claim_must_match_namespace(self):
        claimed = claim_open_entry(environment="live", venue="gate",
                                   inst_id="BTC-USDT-SWAP", side="long",
                                   path=self.path, now_ms=1000)
        self.assertTrue(validate_open_entry_claim(
            claimed[2], environment="live", inst_id="BTC-USDT-SWAP", side="long",
            path=self.path, now_ms=1001))
        self.assertFalse(validate_open_entry_claim(
            claimed[2], environment="live", inst_id="BTC-USDT-SWAP", side="short",
            path=self.path, now_ms=1001))
        self.assertFalse(validate_open_entry_claim(
            "forged", environment="live", inst_id="BTC-USDT-SWAP", side="long",
            path=self.path, now_ms=1001))

    def test_submission_classifier_releases_only_explicit_refusal(self):
        self.assertEqual(classify_entry_submission_exception(ValueError("invalid size")),
                         "rejected_before_send")
        self.assertEqual(classify_entry_submission_exception(TimeoutError("timed out")),
                         "unknown_after_send")

        class HttpRefusal(RuntimeError):
            status = 400

        class RateLimited(RuntimeError):
            status = 429

        self.assertEqual(classify_entry_submission_exception(HttpRefusal("rejected")),
                         "rejected_before_send")
        self.assertEqual(classify_entry_submission_exception(RateLimited("retry")),
                         "unknown_after_send")

    def test_expired_claim_is_reclaimed(self):
        old = claim_open_entry(environment="demo", venue="okx",
                               inst_id="ETH-USDT-SWAP", side="long",
                               path=self.path, now_ms=1000, ttl_seconds=1)
        self.assertTrue(old[0])
        fresh = claim_open_entry(environment="demo", venue="binance",
                                 inst_id="ETH-USDT-SWAP", side="long",
                                 path=self.path, now_ms=2501, ttl_seconds=1)
        self.assertTrue(fresh[0], fresh)
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["claim_id"], fresh[2])


if __name__ == "__main__":
    unittest.main()
