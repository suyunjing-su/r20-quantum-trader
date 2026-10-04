"""Group orchestration, absolute deadline, and Council telemetry contracts."""

import threading
import time
from unittest import mock

import pytest

from astra_backend.council import debate
from astra_backend.council.groups import SymbolGroup


ROLES = {
    "cio": {"name": "首席", "is_arbitrator": True, "prompt": "cio"},
    "trend": {"name": "趋势", "prompt": "trend"},
    "quant": {"name": "量化", "prompt": "quant"},
}
RESOLVED = {
    "model": "M", "base_url": "U", "api_key": "K", "api_format": "F",
    "effort": "high", "requested": "M", "registered": True,
    "fallback": False, "reason": "registered",
}


def _groups():
    return (
        SymbolGroup(0, ("BTC-USDT-SWAP", "ETH-USDT-SWAP"), "GROUP-0 BTC ETH"),
        SymbolGroup(1, ("SOL-USDT-SWAP",), "GROUP-1 SOL"),
    )


def _run_group_debate(call_trader, *, timeout=30.0, cio_call=None):
    def load_config():
        return {"roles": ROLES, "consensus_mode": "standard", "max_concurrency": 4}

    if cio_call is None:
        cio_call = lambda **kwargs: ('{"macro_assessment":"neutral","decisions":{}}', "", {}, 2)

    with mock.patch("astra_backend.llm_manager.execute_llm_request", side_effect=cio_call), \
         mock.patch("astra_backend.llm_manager.get_active_llm_runtime", return_value={"model": "M"}), \
         mock.patch.object(debate, "_normalize_cio_adopted_roles"):
        return debate.execute_council_debate(
            load_config, lambda _spec: dict(RESOLVED), call_trader,
            lambda *args, **kwargs: {}, "FULL-MARKET", "SYSTEM",
            timeout=timeout, runtime_context=None, group_prompts=_groups(),
        )


def _proposal(key, group_prompt):
    return {
        "proposal_id": f"{key}-{group_prompt}", "role_id": key,
        "role_name": key, "status": "ok", "content": group_prompt,
        "weight": 1.0,
    }


def test_group_docket_is_deterministic_and_cio_runs_once():
    calls = []
    cio_calls = []

    def trader(key, spec, group_prompt, *args, **kwargs):
        calls.append((group_prompt, key, kwargs["deadline"]))
        return _proposal(key, group_prompt)

    def cio(**kwargs):
        cio_calls.append(kwargs)
        return ('{"macro_assessment":"one-global-cio","decisions":{}}', "", {}, 1)

    brain, transcript = _run_group_debate(trader, cio_call=cio)

    assert len(calls) == 4
    assert len(cio_calls) == 1
    assert [key for _prompt, key, _deadline in calls] == ["trend", "quant", "trend", "quant"]
    assert [key for key in transcript["advisors"]] == [
        "group_0:trend", "group_0:quant", "group_1:trend", "group_1:quant",
    ]
    assert brain["macro_assessment"] == "one-global-cio"
    telemetry = transcript["telemetry"]
    assert telemetry["group_count"] == 2
    assert telemetry["symbols_per_group"] == [
        ["BTC-USDT-SWAP", "ETH-USDT-SWAP"], ["SOL-USDT-SWAP"],
    ]
    assert all(item["status"] == "ok" for item in telemetry["groups"])
    assert telemetry["trader_stage"]["latency_ms"] >= 0
    assert telemetry["cio_stage"]["start_remaining_budget_ms"] > 0
    assert telemetry["global_council_deadline_ts"] == calls[0][2]


def test_partial_group_failure_is_zero_weight_and_other_group_survives():
    def trader(key, spec, group_prompt, *args, **kwargs):
        if group_prompt.startswith("GROUP-1") and key == "quant":
            raise RuntimeError("provider failure")
        return _proposal(key, group_prompt)

    _brain, transcript = _run_group_debate(trader)

    failed = transcript["advisors"]["group_1:quant"]
    assert failed["status"] == "error"
    assert failed["weight"] == 0.0
    assert "provider failure" in failed["content"]
    assert transcript["advisors"]["group_0:trend"]["status"] == "ok"
    groups = {item["index"]: item for item in transcript["telemetry"]["groups"]}
    assert groups[0]["status"] == "ok"
    assert groups[1]["status"] == "error"
    assert groups[1]["error"] is True


def test_cio_receives_only_remaining_absolute_budget():
    observed = {}

    def trader(key, spec, group_prompt, *args, **kwargs):
        observed.setdefault("deadline", kwargs["deadline"])
        time.sleep(0.01)
        return _proposal(key, group_prompt)

    def cio(**kwargs):
        observed["cio_timeout"] = kwargs["timeout"]
        observed["cio_deadline"] = kwargs["deadline"]
        return ('{"macro_assessment":"ok","decisions":{}}', "", {}, 1)

    _brain, transcript = _run_group_debate(trader, timeout=10.0, cio_call=cio)
    assert observed["cio_deadline"] == observed["deadline"]
    assert observed["cio_timeout"] <= observed["cio_deadline"] - transcript["telemetry"]["cio_stage"]["start_ts"] + 0.01
    assert transcript["telemetry"]["cio_stage"]["end_remaining_budget_ms"] >= 0


def test_bounded_runner_keeps_on_time_results_and_rejects_late_results():
    finished = threading.Event()

    def fast():
        return "fast"

    def slow():
        time.sleep(0.05)
        finished.set()
        return "slow"

    deadline = time.time() + 0.015
    results, unfinished, metrics = debate._bounded_parallel_calls(
        [("fast", (fast,), {}), ("slow", (slow,), {})],
        deadline=deadline, max_workers=2, stagger_seconds=0.0,
    )
    assert results == {"fast": "fast"}
    assert "slow" in unfinished
    assert metrics["fast"]["status"] == "ok"
    finished.wait(1.0)


def test_bounded_runner_cancels_queued_tasks_without_marking_them_as_started():
    started = []
    release = threading.Event()

    def blocking():
        started.append("blocking")
        release.wait(1.0)
        return "blocking"

    def queued():
        started.append("queued")
        return "queued"

    deadline = time.time() + 0.015
    results, unfinished, metrics = debate._bounded_parallel_calls(
        [("blocking", (blocking,), {}), ("queued", (queued,), {})],
        deadline=deadline, max_workers=1, stagger_seconds=0.0,
    )
    assert results == {}
    assert unfinished == {"blocking", "queued"}
    assert started == ["blocking"]
    assert metrics["blocking"]["status"] == "timeout"
    assert metrics["queued"]["status"] == "not_started"
    release.set()


def test_late_completion_from_previous_runner_cannot_enter_a_later_result_set():
    first_finished = threading.Event()

    def late_first():
        time.sleep(0.04)
        first_finished.set()
        return "old-cycle"

    first_deadline = time.time() + 0.01
    old_results, old_unfinished, _ = debate._bounded_parallel_calls(
        [("old", (late_first,), {})],
        deadline=first_deadline, max_workers=1, stagger_seconds=0.0,
    )
    assert old_results == {}
    assert old_unfinished == {"old"}

    new_results, new_unfinished, _ = debate._bounded_parallel_calls(
        [("new", (lambda: "new-cycle",), {})],
        deadline=time.time() + 0.2, max_workers=1, stagger_seconds=0.0,
    )
    assert new_results == {"new": "new-cycle"}
    assert new_unfinished == set()
    assert first_finished.wait(1.0)


def test_council_generation_token_changes_and_old_generation_is_invalidated():
    trader = lambda key, spec, group_prompt, *args, **kwargs: _proposal(key, group_prompt)
    _brain_one, transcript_one = _run_group_debate(trader)
    token_one = transcript_one["cycle_token"]
    assert debate.is_council_cycle_current(token_one)

    _brain_two, transcript_two = _run_group_debate(trader)
    token_two = transcript_two["cycle_token"]
    assert token_two != token_one
    assert debate.is_council_cycle_current(token_two)
    assert not debate.is_council_cycle_current(token_one)
