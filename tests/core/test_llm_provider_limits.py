"""Provider/model concurrency and RPM gates share the Council deadline."""

import threading
import time

import pytest

from astra_backend.llm import transport


def test_provider_model_gate_isolated_by_provider_and_model():
    transport._LLM_GATES.clear()
    first = {"provider_id": "provider-a", "model": "model-a", "concurrency_limit": 1}
    other = {"provider_id": "provider-b", "model": "model-a", "concurrency_limit": 1}
    held = transport._acquire_candidate_gate(first, time.time() + 1.0)
    try:
        independent = transport._acquire_candidate_gate(other, time.time() + 0.1)
        independent.release()
    finally:
        held.release()


def test_rpm_gate_rejects_wait_after_absolute_deadline():
    transport._RATE_BUCKETS.clear()
    candidate = {
        "provider_id": "provider-rpm",
        "model": "model-rpm",
        "rate_limit_per_minute": 1,
    }
    transport._acquire_candidate_rate(candidate, time.time() + 1.0)
    with pytest.raises(transport._LLMTransientError) as caught:
        transport._acquire_candidate_rate(candidate, time.time() + 0.01, threading.Event())
    assert caught.value.timed_out is True


def test_gate_cancellation_does_not_wait_for_provider_permit():
    transport._LLM_GATES.clear()
    candidate = {"provider_id": "provider-cancel", "model": "model", "concurrency_limit": 1}
    held = transport._acquire_candidate_gate(candidate, time.time() + 1.0)
    event = threading.Event()
    event.set()
    try:
        with pytest.raises(transport._LLMTransientError) as caught:
            transport._acquire_candidate_gate(candidate, time.time() + 1.0, event)
        assert caught.value.timed_out is True
    finally:
        held.release()
