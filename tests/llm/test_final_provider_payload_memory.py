"""Offline end-to-end checks for dynamic memory in serialized provider requests.

All provider URLs are reserved .invalid hosts. Every urllib open is intercepted,
request.data is decoded at the final transport boundary, and socket connectors are
blocked as a second network-deny layer. No credentials or production prompts are read.
"""
from __future__ import annotations

import datetime
import http.client
import json
import socket
import tempfile
import urllib.error
import urllib.request
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import scripts.evolution_shield as evolution_shield
import scripts.prompt_library as prompt_library
from scripts.brain.prompt import construct_full_market_prompt
from astra_backend.llm import call as llm_call
from astra_backend.llm import system_one

MEMORY_SENTINEL = "ZEN_WIF_EVOLVING_MEMORY_SENTINEL_20261011"
DISABLED_SENTINEL = "DISABLED_MEMORY_SENTINEL"
EXPIRED_SENTINEL = "EXPIRED_MEMORY_SENTINEL"
CIO_JSON = '{"macro_assessment":"offline","position_management":[],"decisions":{}}'


class _Response:
    def __init__(self, body):
        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._body

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _provider_response(url):
    if url.rstrip("/").endswith("/messages"):
        body = {"content": [{"type": "text", "text": CIO_JSON}],
                "usage": {"input_tokens": 1, "output_tokens": 1}}
    elif url.rstrip("/").endswith("/responses"):
        body = {"output_text": CIO_JSON, "usage": {"input_tokens": 1, "output_tokens": 1}}
    else:
        body = {"choices": [{"message": {"content": CIO_JSON}}],
                "usage": {"total_tokens": 2}}
    return _Response(body)


def _http_error(url, code, body):
    exc = urllib.error.HTTPError(url, code, "offline synthetic failure", {}, None)
    exc.read = lambda: body
    return exc


class FinalProviderPayloadMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="astra-payload-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory_path = self.root / "structured-memory.json"
        p = patch.object(evolution_shield, "STRUCTURED_MEMORY_FILE", self.memory_path)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _lesson(lesson_id, text, *, created_at, enabled=True, ttl_days=90):
        return {"id": lesson_id, "category": "OFFLINE_TEST", "rule_text": text,
                "enabled": enabled, "created_at": created_at, "ttl_days": ttl_days,
                "health_score": 80, "sample_size": 4}

    def _write_valid_memory(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        lessons = []
        for index in range(10):
            stamp = (now - datetime.timedelta(seconds=index)).isoformat()
            text = MEMORY_SENTINEL if index == 0 else f"ACTIVE_MEMORY_{index}"
            lessons.append(self._lesson(f"active-{index}", text, created_at=stamp))
        lessons.extend([
            self._lesson("disabled", DISABLED_SENTINEL, created_at=now.isoformat(), enabled=False),
            self._lesson("expired", EXPIRED_SENTINEL,
                         created_at=(now - datetime.timedelta(days=30)).isoformat(), ttl_days=1),
        ])
        self.memory_path.write_text(json.dumps({
            "schema_version": 1, "revision": "offline-fixture", "lessons": lessons,
        }, ensure_ascii=False), encoding="utf-8")

    def _build_actual_trading_prompt(self):
        context = {}
        profile = {"name": "Offline Payload Test",
                   "trading_user": "动态心法实测：{{trading_memory}}"}
        prompt = construct_full_market_prompt(
            packages=[], pos_summary="offline test account",
            active_positions_detail=[], pending_orders_detail=[],
            current_time_str="2026-10-11 12:00:00 (北京时间)", usdt_available=1000.0,
            runtime_context_out=context,
            safe_float=lambda value, default=0.0: float(value or default),
            sl_atr_mult_for=lambda *_a, **_k: 2.0,
            xvenue_prompt_line=lambda *_a, **_k: "",
            build_risk_budget_text=lambda *_a, **_k: "offline risk budget",
            active_profile=lambda: profile,
            apply_module_layout=prompt_library.apply_module_layout,
            system_version="offline-test",
            ai_memory_md_file=str(self.root / "legacy-memory.md"),
            ai_memory_file=str(self.root / "legacy-memory.json"),
            news_sentiment_file=str(self.root / "news.json"),
            max_leverage=5.0, min_leverage=1.0, max_scale_in_count=3,
            min_scale_in_confidence=70.0, max_margin_equity_ratio=0.25,
            _build_position_lines=lambda *_a, **_k: "",
            _build_pending_order_lines=lambda *_a, **_k: "",
        )
        return prompt, context

    @staticmethod
    def _capture_patch(seen, handler):
        def capture(request, *args, **kwargs):
            if not isinstance(request, urllib.request.Request):
                raise AssertionError("provider transport did not pass Request")
            if not isinstance(request.data, (bytes, bytearray)):
                raise AssertionError("provider Request.data is not serialized bytes")
            payload = json.loads(bytes(request.data).decode("utf-8"))
            seen.append({"url": request.full_url, "payload": payload})
            return handler(request.full_url, payload)
        return capture

    def _network_fence(self, stack, seen, handler):
        stack.enter_context(patch("urllib.request.urlopen",
                                  side_effect=self._capture_patch(seen, handler)))
        stack.enter_context(patch("socket.create_connection",
                                  side_effect=AssertionError("network denied")))
        stack.enter_context(patch.object(http.client.HTTPConnection, "connect",
                                         side_effect=AssertionError("network denied")))
        stack.enter_context(patch.object(http.client.HTTPSConnection, "connect",
                                         side_effect=AssertionError("network denied")))

    @staticmethod
    def _serialized(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _run_wire(self, messages, runtime, *, resolve_runtime=None, allow_fallback=False,
                  handler=None):
        seen = []
        with ExitStack() as stack:
            self._network_fence(stack, seen, handler or
                                (lambda url, _payload: _provider_response(url)))
            result = llm_call.execute_llm_request(
                lambda: runtime, resolve_runtime or (lambda _model: None),
                lambda _event: None, messages,
                allow_fallback=allow_fallback, timeout=1.0,
                response_format={"type": "json_object"}, temperature=0.2,
            )
        return result, seen

    def test_structured_memory_reread_reaches_each_serialized_chat_protocol(self):
        self._write_valid_memory()
        prompt, context = self._build_actual_trading_prompt()
        self.assertIn(MEMORY_SENTINEL, prompt)
        self.assertIn(MEMORY_SENTINEL, context["trading_memory"])
        for filtered in (DISABLED_SENTINEL, EXPIRED_SENTINEL,
                         "ACTIVE_MEMORY_8", "ACTIVE_MEMORY_9"):
            self.assertNotIn(filtered, prompt)
        messages = [{"role": "system", "content": "offline policy"},
                    {"role": "user", "content": prompt}]
        for api_format in ("openai_chat", "openai_responses", "claude_messages"):
            with self.subTest(api_format=api_format):
                runtime = {
                    "model": f"offline-{api_format}",
                    "base_url": "https://provider.invalid/v1",
                    "api_key": "FAKE_TEST_VALUE_NOT_A_CREDENTIAL",
                    "api_format": api_format, "reasoning_effort": "low",
                    "reasoning_type": "auto", "request_attempts": 1,
                    "fallback_model_ids": [], "max_tokens": 256,
                }
                (content, _reasoning, _usage, _latency), seen = self._run_wire(messages, runtime)
                self.assertEqual(content, CIO_JSON)
                self.assertEqual(len(seen), 1)
                payload = seen[0]["payload"]
                self.assertIn(MEMORY_SENTINEL, self._serialized(payload))
                self.assertNotIn("FAKE_TEST_VALUE_NOT_A_CREDENTIAL", self._serialized(payload))
                if api_format == "openai_responses":
                    self.assertIn(MEMORY_SENTINEL, self._serialized(payload["input"]))
                elif api_format == "claude_messages":
                    self.assertIn(MEMORY_SENTINEL,
                                  payload["system"] + self._serialized(payload["messages"]))
                else:
                    self.assertIn(MEMORY_SENTINEL, self._serialized(payload["messages"]))

    def test_adaptive_parameter_retry_serializes_memory_on_both_requests(self):
        self._write_valid_memory()
        prompt, _context = self._build_actual_trading_prompt()
        runtime = {
            "model": "gpt-5-offline", "base_url": "https://adaptive.invalid/v1",
            "api_key": "FAKE_TEST_VALUE", "api_format": "openai_chat",
            "reasoning_effort": "high", "reasoning_type": "standard_effort",
            "request_attempts": 1, "fallback_model_ids": [], "max_tokens": 256,
        }
        seen = []

        def handler(url, _payload):
            if len(seen) == 1:
                raise _http_error(url, 400,
                                  b'{"error":"invalid parameter reasoning_effort"}')
            return _provider_response(url)

        with ExitStack() as stack:
            self._network_fence(stack, seen, handler)
            content, _reasoning, _usage, _latency = llm_call.execute_llm_request(
                lambda: runtime, lambda _model: None, lambda _event: None,
                [{"role": "user", "content": prompt}],
                allow_fallback=False, timeout=1.0,
            )
        self.assertEqual(content, CIO_JSON)
        self.assertEqual(len(seen), 2)
        for request in seen:
            self.assertIn(MEMORY_SENTINEL, self._serialized(request["payload"]))
        self.assertIn("reasoning_effort", seen[0]["payload"])
        self.assertNotIn("reasoning_effort", seen[1]["payload"])

    def test_provider_model_fallback_serializes_memory_to_primary_and_backup(self):
        self._write_valid_memory()
        prompt, _context = self._build_actual_trading_prompt()
        primary = {
            "model": "primary-offline", "base_url": "https://primary.invalid/v1",
            "api_key": "FAKE_PRIMARY_KEY", "api_format": "openai_chat",
            "reasoning_effort": "low", "request_attempts": 1,
            "fallback_model_ids": ["backup-offline"], "max_tokens": 256,
        }
        backup = {
            "model": "backup-offline", "base_url": "https://backup.invalid/v1",
            "api_key": "FAKE_BACKUP_KEY", "api_format": "openai_responses",
            "reasoning_effort": "low", "request_attempts": 1, "max_tokens": 256,
        }
        seen = []

        def handler(url, _payload):
            if "primary.invalid" in url:
                raise _http_error(url, 401, b'{"error":"synthetic auth rejection"}')
            return _provider_response(url)

        failovers = []
        with ExitStack() as stack:
            self._network_fence(stack, seen, handler)
            content, _reasoning, _usage, _latency = llm_call.execute_llm_request(
                lambda: primary,
                lambda model: backup if model == "backup-offline" else None,
                failovers.append, [{"role": "user", "content": prompt}],
                allow_fallback=True, timeout=1.0,
            )
        self.assertEqual(content, CIO_JSON)
        self.assertEqual(len(seen), 2)
        self.assertIn("primary.invalid", seen[0]["url"])
        self.assertIn("backup.invalid", seen[1]["url"])
        for request in seen:
            payload_text = self._serialized(request["payload"])
            self.assertIn(MEMORY_SENTINEL, payload_text)
            self.assertNotIn("FAKE_", payload_text)
        self.assertTrue(any(event.get("type") == "fallback_hit" for event in failovers))

    def test_corrupt_structured_memory_fails_before_any_provider_request(self):
        self.memory_path.write_text('{"schema_version":99,"lessons":[]}', encoding="utf-8")
        seen = []
        with ExitStack() as stack:
            self._network_fence(stack, seen,
                                lambda url, _payload: _provider_response(url))
            with self.assertRaises(evolution_shield.MemoryCorruptError):
                self._build_actual_trading_prompt()
        self.assertEqual(seen, [])

    def test_system_one_final_request_uses_typed_state_not_chat_messages(self):
        seen = []
        with ExitStack() as stack:
            stack.enter_context(patch("urllib.request.urlopen",
                side_effect=self._capture_patch(seen, lambda _url, _payload: _Response({
                    "model": "offline-system-one",
                    "answers": {"protective_action": {"selected": "HOLD"}},
                    "usage": {"input_tokens": 2},
                }))))
            stack.enter_context(patch("socket.create_connection",
                                      side_effect=AssertionError("network denied")))
            stack.enter_context(patch.object(http.client.HTTPSConnection, "connect",
                                             side_effect=AssertionError("network denied")))
            result = system_one.execute_system_one_request(
                model="offline-system-one",
                state={"schema": "test", "snapshot": {"market": {"sentinel": MEMORY_SENTINEL}}},
                questions={"protective_action": {"type": "choice", "criteria": {"HOLD": None}}},
                base_url="https://typesafe.invalid/v1", api_key="FAKE_SYSTEM_ONE_KEY",
                timeout=1.0, attempts=1,
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(seen), 1)
        payload = seen[0]["payload"]
        self.assertEqual(set(payload), {"model", "state", "questions"})
        self.assertIn(MEMORY_SENTINEL, self._serialized(payload["state"]))
        self.assertNotIn("FAKE_SYSTEM_ONE_KEY", self._serialized(payload))
        self.assertEqual(seen[0]["url"], "https://typesafe.invalid/v1/systemone")

    def test_council_trader_critique_and_cio_payloads_include_rendered_memory(self):
        self._write_valid_memory()
        prompt, context = self._build_actual_trading_prompt()
        from astra_backend import llm_manager
        from astra_backend.council import debate

        runtime = {
            "model": "offline-council", "base_url": "https://council.invalid/v1",
            "api_key": "FAKE_COUNCIL_KEY", "api_format": "openai_chat",
            "reasoning_effort": "low", "reasoning_type": "auto",
            "request_attempts": 1, "fallback_model_ids": [], "max_tokens": 256,
        }
        roles = {
            "trader_test": {"name": "测试参谋", "enabled": True,
                             "prompt": "交易原则：{{trading_memory}}", "temperature": 0.0},
            "cio": {"name": "测试 CIO", "is_arbitrator": True,
                    "prompt": "终审必须遵循：{{trading_memory}}", "temperature": 0.0},
        }
        config = {"roles": roles, "consensus_mode": "cross_examination"}

        def resolve_seat(_spec):
            return {"model": runtime["model"], "base_url": runtime["base_url"],
                    "api_key": runtime["api_key"], "api_format": runtime["api_format"],
                    "effort": "low", "requested": runtime["model"],
                    "registered": True, "fallback": False, "reason": "offline fixture",
                    "max_tokens": 256}

        call_trader = lambda *a, **k: debate._call_single_trader(resolve_seat, *a, **k)
        call_critique = lambda *a, **k: debate._call_single_trader_critique(resolve_seat, *a, **k)
        seen = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(llm_manager, "get_active_llm_runtime",
                                             return_value=runtime))
            stack.enter_context(patch.object(llm_manager, "resolve_model_runtime",
                                             return_value=None))
            stack.enter_context(patch.object(llm_manager, "record_failover_event"))
            self._network_fence(stack, seen,
                                lambda url, _payload: _provider_response(url))
            brain, transcript = debate.execute_council_debate(
                lambda: config, resolve_seat, call_trader, call_critique,
                prompt, "offline system policy", timeout=180.0, runtime_context=context,
            )
        self.assertIn("decisions", brain)
        self.assertEqual(transcript["consensus_mode"], "cross_examination")
        self.assertEqual(len(seen), 3, "trader, critique, and CIO each serialize one request")
        for request in seen:
            payload_text = self._serialized(request["payload"]["messages"])
            self.assertIn(MEMORY_SENTINEL, payload_text)
            self.assertNotIn("FAKE_COUNCIL_KEY", self._serialized(request["payload"]))

    def test_direct_urllib_brain_fallback_is_captured_at_request_data(self):
        """Exercise dispatch's direct urllib branch with unified LLM calls disabled."""
        import contextlib
        import os
        import time
        import urllib
        from astra_backend import council_manager, file_locks
        from scripts.brain.dispatch import dispatch_llm_and_persist_decisions

        seen = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(council_manager, "load_council_config",
                                             return_value={"enabled": False}))
            stack.enter_context(patch.object(file_locks, "file_lock",
                                             lambda *_a, **_k: contextlib.nullcontext()))
            self._network_fence(stack, seen, lambda url, _payload: _provider_response(url))
            result = dispatch_llm_and_persist_decisions(
                AI_DECISION_CACHE_FILE=str(self.root / "cache.json"),
                AI_DECISION_HISTORY_FILE=str(self.root / "history.json"),
                AI_POSITION_MANAGEMENT_FILE=str(self.root / "positions.json"),
                Any=object, Dict=dict,
                _build_effective_prompt_text=lambda **_k: "offline history",
                _build_history_record=lambda **_k: {},
                _normalize_position_management=lambda items, *_a, **_k: items,
                _record_cycle_health=lambda *_a, **_k: None,
                active_inst_ids=set(), active_position_sides={},
                api_format="openai_chat", api_key="FAKE_DIRECT_KEY",
                assemble_decision_cache=lambda **_k: {"offline": True},
                atomic_write_json=lambda *_a, **_k: None,
                base_url="https://direct.invalid/v1", effective_system_prompt="offline system",
                effort="low", execute_brain_pending_cancels=lambda *_a: None,
                execute_llm_request=None, json=json, model_name="offline-direct", os=os,
                packages=[], policy_hash="", policy_snapshot={}, policy_summary="",
                policy_version="test", prompt=f"direct prompt {MEMORY_SENTINEL}",
                runtime_context={}, safe_float=lambda value: float(value or 0),
                telemetry=type("Telemetry", (), {"finish": lambda *_a, **_k: None})(),
                thinking_timeout=1.0, time=time, time_str="2026-10-11 12:00:00",
                urllib=urllib,
            )
        self.assertEqual(result, {"offline": True})
        self.assertEqual(len(seen), 1)
        payload_text = self._serialized(seen[0]["payload"])
        self.assertIn(MEMORY_SENTINEL, payload_text)
        self.assertNotIn("FAKE_DIRECT_KEY", payload_text)

    def test_self_improvement_direct_urllib_serializes_effective_review_prompts(self):
        import importlib
        import sys
        import types
        from astra_backend import evolution_config, llm_manager

        fcntl_stub = types.ModuleType("fcntl")
        fcntl_stub.LOCK_EX = 1
        fcntl_stub.LOCK_NB = 2
        fcntl_stub.LOCK_UN = 4
        fcntl_stub.flock = lambda *_args, **_kwargs: None
        scripts_root = str(Path(__file__).resolve().parents[2] / "scripts")
        with patch.dict(sys.modules, {"fcntl": fcntl_stub}), \
                patch.object(sys, "path", [scripts_root, *sys.path]):
            self_improvement_engine = importlib.import_module(
                "scripts.self_improvement_engine")

        runtime = {
            "model": "offline-self-evolution", "base_url": "https://evolution.invalid/v1",
            "api_key": "FAKE_EVOLUTION_KEY", "api_format": "openai_chat",
            "reasoning_effort": "low", "thinking_timeout": 1.0,
        }
        seen = []

        class _Telemetry:
            def __init__(self, *_args, **_kwargs):
                pass
            def finish(self, *_args, **_kwargs):
                pass

        with ExitStack() as stack:
            stack.enter_context(patch.object(self_improvement_engine,
                "get_cpa_client_config", return_value=("https://evolution.invalid/v1", "FAKE_EVOLUTION_KEY")))
            stack.enter_context(patch.object(self_improvement_engine,
                "compose_evolution_prompts", return_value=(
                    f"effective system {MEMORY_SENTINEL}",
                    f"effective user {MEMORY_SENTINEL}",
                    "2026-10-11 12:00:00", {})))
            stack.enter_context(patch.object(self_improvement_engine, "DATA_DIR", str(self.root)))
            stack.enter_context(patch.object(self_improvement_engine, "EVOLUTION_LAST_PROMPT_FILE",
                                             str(self.root / "last-prompt.txt")))
            stack.enter_context(patch.object(self_improvement_engine, "ModelCallTelemetry", _Telemetry))
            stack.enter_context(patch.object(self_improvement_engine, "log_msg"))
            stack.enter_context(patch.object(evolution_config, "load_evolution_config", return_value={}))
            stack.enter_context(patch.object(evolution_config, "resolve_evolution_llm_runtime",
                                             return_value={}))
            stack.enter_context(patch.object(llm_manager, "get_active_llm_runtime",
                                             return_value=runtime))
            stack.enter_context(patch.object(llm_manager, "execute_llm_request", None))
            self._network_fence(stack, seen,
                                lambda url, _payload: _provider_response(url))
            result = self_improvement_engine.call_llm_evolution_review(
                [], existing_memory_md="offline fixture", timestamp_str="2026-10-11 12:00:00")

        self.assertIsInstance(result, dict)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["url"], "https://evolution.invalid/v1/chat/completions")
        payload_text = self._serialized(seen[0]["payload"])
        self.assertIn(MEMORY_SENTINEL, payload_text)
        self.assertNotIn("FAKE_EVOLUTION_KEY", payload_text)


if __name__ == "__main__":
    unittest.main()
