from astra_backend.fast_decision_policy import build_state, normalize_action, parse_decision, validate_decision
from astra_backend.llm.system_one import build_system_one_request, parse_system_one_response


def test_system_one_request_is_not_chat_payload():
    endpoint, headers, payload = build_system_one_request(
        model="jev-1.13.0",
        state={"market": {}},
        questions={"action": {"type": "choice", "criteria": {"options": ["HOLD"]}}},
        base_url="https://api.typesafe.ai/v1",
        api_key="test-only-not-a-credential",
    )
    assert endpoint.endswith("/v1/systemone")
    assert headers["Authorization"].startswith("Bearer ")
    assert set(payload) == {"model", "state", "questions"}
    assert "messages" not in payload


def test_system_one_response_preserves_typed_answers():
    result = parse_system_one_response({
        "model": "jev-1.13.0",
        "answers": {"action": {"selected": "HOLD"}},
        "usage": {"input_tokens": 2, "output_tokens": 3},
    })
    assert result["answers"]["action"]["selected"] == "HOLD"
    assert result["usage"]["total_tokens"] == 5


def test_fast_decision_questions_use_supported_choice_types():
    from astra_backend.fast_decision_policy import build_typed_questions

    questions = build_typed_questions()
    assert set(questions) == {"protective_action", "protection_reason", "reduce_fraction"}
    assert all(question["type"] == "choice" for question in questions.values())
    assert set(questions["protection_reason"]["criteria"]) == {"true", "false"}


def test_fast_decision_unknown_action_fails_safe_to_hold():
    decision = parse_decision({"answers": {"protective_action": "BUY_LONG"}}, received_at=100.0)
    assert decision["action"] == "HOLD"
    assert normalize_action({"selected": "EMERGENCY-FLATTEN"}) == "EMERGENCY_FLATTEN"


def test_fast_decision_ttl_and_tighten_price_guard():
    stale = {"action": "EMERGENCY_FLATTEN", "received_at": 1}
    assert validate_decision(stale, {}, now=100, ttl_seconds=45) == (False, "decision_stale")
    fresh = {"action": "TIGHTEN_PROTECTION", "received_at": 100}
    assert validate_decision(fresh, {}, now=101, ttl_seconds=45) == (False, "tighten_price_missing")
    assert validate_decision(fresh, {"tighten_stop_price": 10}, now=101, ttl_seconds=45) == (True, "ok")


def test_fast_decision_state_has_no_credentials():
    state = build_state({"api_key": "secret", "positions": []}, generated_at=100)
    assert "api_key" not in str(state)


def test_reduce_only_decision_requires_bounded_fraction():
    from astra_backend.fast_decision_policy import parse_decision, validate_decision

    decision = parse_decision({"answers": {"protective_action": "REDUCE_ONLY"}}, received_at=100.0)
    assert decision["reduce_fraction"] == 0.0
    assert validate_decision(decision, {}, now=101.0, ttl_seconds=45) == (
        False,
        "reduce_fraction_out_of_bounds",
    )

    decision = parse_decision(
        {"answers": {
            "protective_action": "REDUCE_ONLY",
            "reduce_fraction": {"selected": "0.50"},
        }},
        received_at=100.0,
    )
    assert validate_decision(decision, {}, now=101.0, ttl_seconds=45) == (True, "ok")


def test_fast_decision_reduce_only_uses_bounded_execution_callback(monkeypatch):
    import astra_backend.fast_decision_service as service

    monkeypatch.setattr(service, "claim_action", lambda *args, **kwargs: True)
    calls = []
    result = service.execute_protective_decision(
        {"action": "REDUCE_ONLY", "reduce_fraction": 0.5, "received_at": 100.0},
        {"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": 10, "venue": "okx"},
        snapshot={},
        ai_tightens_stop=lambda *_args: False,
        close_position_confirmed=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full close used")),
        amend_venue_stop_loss=lambda *_args, **_kwargs: None,
        reduce_only_position=lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "ok": True,
            "detail": "submitted",
            "reduce_only": True,
        },
        venue_registry=None,
        current_environment=lambda: None,
        re_read_position=lambda *_args: {"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": 10},
        now=101.0,
    )
    assert result["ok"] is True
    assert result["reduce_only"] is True
    assert result["quantity"] == 5.0
    assert calls == [(
        ("BTC-USDT-SWAP", 5.0),
        {"venue": "okx", "pos_side": "long"},
    )]


def test_execution_router_reduce_only_sends_explicit_quantity_and_flag(monkeypatch):
    from types import SimpleNamespace
    from astra_backend import execution_router

    class Adapter:
        capabilities = SimpleNamespace(venue="gate", decimal_amount=False)
        environment = "live"

        def native_symbol(self, symbol):
            return "BTC_USDT"

        def positions(self):
            return [{"inst_id": "BTC_USDT", "side": "long", "size_signed": 10}]

        def place_order(self, *args, **kwargs):
            self.order = (args, kwargs)
            return {"id": "order-1"}

    adapter = Adapter()
    monkeypatch.setattr(execution_router, "require_execution", lambda *args, **kwargs: None)
    result = execution_router.reduce_only_position(
        "BTC-USDT-SWAP", 5, venue="gate", pos_side="long", adapter=adapter,
    )
    assert result["ok"] is True
    assert result["reduce_only"] is True
    assert adapter.order[0][1] == "sell"
    assert adapter.order[0][2] == 5
    assert adapter.order[1]["reduce_only"] is True


def test_reduce_only_does_not_pass_through_entry_wait_normalization():
    from astra_backend.fast_decision_policy import parse_decision

    decision = parse_decision({
        "answers": {
            "protective_action": {"type": "choice", "choice": "REDUCE_ONLY"},
            "reduce_fraction": {"type": "choice", "choice": "0.25"},
        }
    }, received_at=100.0)
    assert decision["action"] == "REDUCE_ONLY"
    assert decision["action"] != "WAIT"


def test_reduce_only_router_rejects_position_shrink_between_decision_and_send(monkeypatch):
    from types import SimpleNamespace
    from astra_backend import execution_router

    class ShrunkAdapter:
        capabilities = SimpleNamespace(venue="gate", decimal_amount=False)
        environment = "live"

        def __init__(self):
            self.sent = False

        def native_symbol(self, symbol):
            return "BTC_USDT"

        def positions(self):
            return [{"inst_id": "BTC_USDT", "side": "long", "size_signed": 2}]

        def place_order(self, *args, **kwargs):
            self.sent = True
            return {"id": "should-not-send"}

    adapter = ShrunkAdapter()
    monkeypatch.setattr(execution_router, "require_execution", lambda *args, **kwargs: None)
    result = execution_router.reduce_only_position(
        "BTC-USDT-SWAP", 5, venue="gate", pos_side="long", adapter=adapter,
    )
    assert result["ok"] is False
    assert result["stage"] == "reduce_only_validate"
    assert adapter.sent is False


def test_emergency_flatten_and_reduce_only_use_separate_callbacks(monkeypatch):
    import astra_backend.fast_decision_service as service

    monkeypatch.setattr(service, "claim_action", lambda *args, **kwargs: True)
    calls = []
    common = dict(
        snapshot={},
        ai_tightens_stop=lambda *_args: False,
        close_position_confirmed=lambda *args, **kwargs: calls.append(("flatten", args, kwargs)) or (True, "flat"),
        amend_venue_stop_loss=lambda *_args, **_kwargs: None,
        reduce_only_position=lambda *args, **kwargs: calls.append(("reduce", args, kwargs)) or {"ok": True},
        venue_registry=None,
        current_environment=lambda: None,
        re_read_position=lambda *_args: {"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": 4},
        now=101.0,
    )
    service.execute_protective_decision(
        {"action": "REDUCE_ONLY", "reduce_fraction": 0.25, "received_at": 100},
        {"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": 4, "venue": "okx"},
        **common,
    )
    service.execute_protective_decision(
        {"action": "EMERGENCY_FLATTEN", "received_at": 100},
        {"instId": "BTC-USDT-SWAP", "posSide": "long", "pos": 4, "venue": "okx"},
        **common,
    )
    assert [call[0] for call in calls] == ["reduce", "flatten"]
    assert calls[0][1][1] == 1.0
    assert calls[1][1][2] == 4.0


def test_fast_decision_event_lane_coalesces_latest_market_event(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from astra_gateway.scheduler import GatewayScheduler
    from astra_gateway.store import GatewayStore
    import astra_gateway.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "FAST_DECISION_EVENT_FILE", tmp_path / "market-event.json")
    scheduler = GatewayScheduler(GatewayStore(tmp_path / "gateway.db"))
    scheduler.fast_decision_executor = MagicMock()
    running_future = MagicMock()
    running_future.done.return_value = False
    scheduler.fast_decision_executor.submit.return_value = running_future
    try:
        first = {"venue": "okx", "symbol": "BTC-USDT-SWAP", "price": 100.0, "received_timestamp": 100.0}
        second = {"venue": "okx", "symbol": "BTC-USDT-SWAP", "price": 100.4, "received_timestamp": 100.1}
        assert scheduler.trigger_fast_decision_event(first) is True
        assert scheduler.trigger_fast_decision_event(second) is True
        assert scheduler.fast_decision_executor.submit.call_count == 1
        assert scheduler._fast_event_pending is True
    finally:
        scheduler.shutdown()
