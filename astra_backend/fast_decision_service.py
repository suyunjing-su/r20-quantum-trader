"""Fast Decision orchestration: typed call, stale guard, and protective boundary."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Mapping

from astra_backend.fast_decision_policy import (
    ALLOWED_ACTIONS,
    build_state,
    build_typed_questions,
    parse_decision,
    validate_decision,
)
from astra_backend.fast_decision_store import (
    claim_action,
    entry_block_status,
    record_latency_sample,
    set_entry_block,
)
from astra_backend.llm.system_one import execute_system_one_request


def evaluate(snapshot: Mapping[str, Any], runtime: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Make one bounded System One call. This function never executes an action."""
    trigger_at = time.time()
    now_v = trigger_at if now is None else float(now)
    state = build_state(snapshot, generated_at=now_v)
    state_built_at = time.time()
    result = execute_system_one_request(
        model=str(runtime.get("model") or ""),
        state=state,
        questions=build_typed_questions(),
        base_url=str(runtime.get("base_url") or ""),
        api_key=str(runtime.get("api_key") or ""),
        api_path=str(runtime.get("api_path") or "/systemone"),
        timeout=float(runtime.get("timeout_seconds") or 8.0),
        attempts=2,
    )
    response_received_at = float(result.get("response_received_at") or time.time())
    request_sent_at = float(result.get("request_sent_at") or state_built_at)
    event_at = snapshot.get("market_event_timestamp") or snapshot.get("event_timestamp")
    timestamps: dict[str, float | None] = {
        "market_event_timestamp": None,
        "fast_decision_trigger_timestamp": trigger_at,
        "snapshot_built_timestamp": state_built_at,
        "request_sent_timestamp": request_sent_at,
        "response_received_timestamp": response_received_at,
        "policy_timestamp": None,
        "execution_start_timestamp": None,
        "order_submitted_timestamp": None,
    }
    try:
        if event_at is not None:
            timestamps["market_event_timestamp"] = float(event_at)
    except (TypeError, ValueError):
        pass
    sample = {
        "trigger_to_request_ms": max(0.0, (request_sent_at - trigger_at) * 1000),
        "system_one_request_response_ms": max(0.0, (response_received_at - request_sent_at) * 1000),
    }
    if timestamps["market_event_timestamp"] is not None:
        event_timestamp = float(timestamps["market_event_timestamp"])
        sample["event_to_trigger_ms"] = max(0.0, (trigger_at - event_timestamp) * 1000)
        sample["event_to_decision_ms"] = max(0.0, (response_received_at - event_timestamp) * 1000)
    if not result.get("ok"):
        record_latency_sample(sample)
        return {
            "ok": False,
            "action": "HOLD",
            "failure_safe": True,
            "error": result.get("error", "System One request failed"),
            "status_code": result.get("status_code", 0),
            "latency": sample,
            "timestamps": timestamps,
        }
    decision = parse_decision(result, received_at=response_received_at)
    ok, reason = validate_decision(decision, snapshot, now=response_received_at, ttl_seconds=float(runtime.get("decision_ttl_seconds") or 45.0))
    policy_completed_at = time.time()
    timestamps["policy_timestamp"] = policy_completed_at
    sample["response_to_policy_ms"] = max(0.0, (policy_completed_at - response_received_at) * 1000)
    sample["trigger_to_policy_ms"] = max(0.0, (policy_completed_at - trigger_at) * 1000)
    if timestamps["market_event_timestamp"] is not None:
        sample["event_to_policy_ms"] = max(
            0.0, (policy_completed_at - float(timestamps["market_event_timestamp"])) * 1000
        )
    record_latency_sample(sample)
    if not ok:
        return {"ok": False, "action": "HOLD", "failure_safe": True, "error": reason, "decision": decision, "latency": sample, "timestamps": timestamps}
    return {"ok": True, "action": decision["action"], "decision": decision, "state": state, "latency": sample, "timestamps": timestamps}


def _action_id(decision: Mapping[str, Any], position: Mapping[str, Any]) -> str:
    material = json.dumps({
        "action": decision.get("action"),
        "received_at": decision.get("received_at"),
        "inst_id": position.get("instId"),
        "venue": position.get("venue") or position.get("exchange") or "okx",
        "side": position.get("posSide") or position.get("side"),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def execute_protective_decision(
    decision: Mapping[str, Any],
    position: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    ai_tightens_stop: Callable[[Mapping[str, Any], Mapping[str, Any]], bool],
    close_position_confirmed: Callable[..., Any],
    amend_venue_stop_loss: Callable[..., Any],
    reduce_only_position: Callable[..., Any] | None = None,
    venue_registry: Any,
    current_environment: Callable[[], Any],
    re_read_position: Callable[[str, str, str], Mapping[str, Any] | None],
    dedup_seconds: float = 60.0,
    ttl_seconds: float = 45.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply only protective actions through existing venue-aware callbacks.

    The callback boundary is intentional: this module has no exchange client and
    cannot open, reverse, scale in, change leverage, or bypass interceptors.
    """
    execution_started_at = time.time()
    now_v = execution_started_at if now is None else float(now)
    action = str(decision.get("action") or "HOLD").upper()
    if action not in ALLOWED_ACTIONS:
        return {"ok": False, "action": "HOLD", "error": "action_not_allowed", "failure_safe": True}
    if action == "HOLD":
        return {"ok": True, "action": "HOLD", "executed": False}
    received = float(decision.get("received_at", 0) or 0)
    if received <= 0 or now_v - received > max(1.0, ttl_seconds):
        return {"ok": False, "action": "HOLD", "error": "decision_stale", "failure_safe": True}
    if action == "DISABLE_NEW_ENTRY":
        action_id = _action_id(decision, {})
        if not claim_action(action_id, now=now_v, dedup_seconds=dedup_seconds):
            return {"ok": True, "action": action, "executed": False, "deduplicated": True}
        ttl = max(1.0, ttl_seconds)
        set_entry_block(expires_at=now_v + ttl, reason=str(decision.get("reason") or "Fast Decision protective block"))
        return {"ok": True, "action": action, "executed": True, "expires_at": now_v + ttl}

    if not isinstance(position, Mapping) or not position.get("instId"):
        return {"ok": False, "action": "HOLD", "error": "position_missing", "failure_safe": True}
    action_id = _action_id(decision, position)
    if not claim_action(action_id, now=now_v, dedup_seconds=dedup_seconds):
        return {"ok": True, "action": action, "executed": False, "deduplicated": True}

    inst_id = str(position.get("instId"))
    venue = str(position.get("venue") or position.get("exchange") or "okx").lower()
    pos_side = str(position.get("posSide") or position.get("side") or "net").lower()
    # Re-read immediately before every action. A missing/changed position is safe.
    latest = re_read_position(inst_id, pos_side, venue)
    if not latest:
        return {"ok": False, "action": "HOLD", "error": "position_reread_failed_or_flat", "failure_safe": True}

    if action == "TIGHTEN_PROTECTION":
        instruction = dict(decision)
        instruction["suggested_sl_price"] = snapshot.get("tighten_stop_price")
        if not ai_tightens_stop(instruction, latest):
            return {"ok": False, "action": "HOLD", "error": "stop_not_tightening", "failure_safe": True}
        try:
            adapter_env = __import__("astra_backend.close_intent", fromlist=["adapter_environment"]).adapter_environment
            adapter = venue_registry.get_adapter(venue, environment=adapter_env(venue, str(current_environment().mode)))
            ok, note = amend_venue_stop_loss(adapter, inst_id.replace("-USDT-SWAP", ""), pos_side, float(snapshot["tighten_stop_price"]), abs(float(latest.get("pos", 0) or 0)))
            execution_sent_at = time.time()
            return {
                "ok": bool(ok),
                "action": action if ok else "HOLD",
                "executed": bool(ok),
                "detail": str(note),
                "execution_started_at": execution_started_at,
                "execution_sent_at": execution_sent_at,
            }
        except Exception as exc:
            return {"ok": False, "action": "HOLD", "error": f"tighten_failed: {exc}", "failure_safe": True}

    if action == "REDUCE_ONLY":
        if reduce_only_position is None:
            return {"ok": False, "action": "HOLD", "error": "reduce_only_boundary_unavailable", "failure_safe": True}
        try:
            fraction = float(decision.get("reduce_fraction", 0) or 0)
            current_size = abs(float(latest.get("pos", 0) or 0))
            quantity = current_size * fraction
            if not (0 < fraction <= 0.5) or quantity <= 0:
                return {"ok": False, "action": "HOLD", "error": "reduce_fraction_out_of_bounds", "failure_safe": True}
            execution_sent_at = time.time()
            result = reduce_only_position(
                inst_id,
                quantity,
                venue=venue,
                pos_side=pos_side,
            )
            if isinstance(result, tuple):
                reduced, detail = bool(result[0]), result[1] if len(result) > 1 else ""
            elif isinstance(result, Mapping):
                reduced, detail = bool(result.get("ok")), result.get("detail") or result
            else:
                reduced, detail = bool(result), result
            return {
                "ok": reduced,
                "action": action if reduced else "HOLD",
                "executed": reduced,
                "quantity": quantity,
                "reduce_only": True,
                "detail": str(detail),
                "execution_started_at": execution_started_at,
                "execution_sent_at": execution_sent_at,
            }
        except Exception as exc:
            return {"ok": False, "action": "HOLD", "error": f"reduce_only_failed: {exc}", "failure_safe": True}

    if action == "EMERGENCY_FLATTEN":
        try:
            execution_sent_at = time.time()
            closed, detail = close_position_confirmed(inst_id, pos_side, float(latest.get("pos", 0) or 0), venue=venue)
            return {"ok": bool(closed), "action": action if closed else "HOLD", "executed": bool(closed), "detail": str(detail), "execution_started_at": execution_started_at, "execution_sent_at": execution_sent_at}
        except Exception as exc:
            return {"ok": False, "action": "HOLD", "error": f"emergency_flatten_failed: {exc}", "failure_safe": True}

    return {"ok": False, "action": "HOLD", "error": "unsupported_protective_action", "failure_safe": True}


def entry_block() -> dict[str, Any]:
    return entry_block_status()
