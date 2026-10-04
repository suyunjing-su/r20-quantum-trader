"""Fast Decision / Risk Guardian policy boundary.

This module is deliberately independent from Council roles.  It accepts only
protective actions and treats every malformed or unknown answer as HOLD.
"""
from __future__ import annotations

import math
import time
from typing import Any, Mapping

ALLOWED_ACTIONS = frozenset({
    "HOLD",
    "TIGHTEN_PROTECTION",
    "REDUCE_ONLY",
    "EMERGENCY_FLATTEN",
    "DISABLE_NEW_ENTRY",
})
FORBIDDEN_ACTIONS = frozenset({
    "BUY_LONG", "SELL_SHORT", "OPEN", "REVERSE", "ADD", "SCALE_IN",
    "INCREASE_LEVERAGE", "REMOVE_STOP", "LOOSEN_PROTECTION",
})


def _answer_value(answer: Any) -> Any:
    if isinstance(answer, Mapping):
        for key in ("selected", "value", "answer", "choice", "label"):
            if key in answer:
                return answer[key]
    return answer


def normalize_action(answer: Any) -> str:
    value = _answer_value(answer)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else "HOLD"
    action = str(value or "HOLD").strip().upper().replace("-", "_").replace(" ", "_")
    return action if action in ALLOWED_ACTIONS else "HOLD"


def finite_positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def build_typed_questions() -> dict[str, dict[str, Any]]:
    """Questions are fixed and typed; model output cannot invent an order API."""
    return {
        "protective_action": {
            "type": "choice",
            "instructions": "Choose one protective action only.",
            "criteria": {action: None for action in sorted(ALLOWED_ACTIONS)},
        },
        "protection_reason": {
            "type": "choice",
            "instructions": "Choose whether an immediate protective intervention is required.",
            "criteria": {
                "true": "The current state requires immediate protective action.",
                "false": "No immediate protective action is required.",
            },
        },
        "reduce_fraction": {
            "type": "choice",
            "instructions": "For REDUCE_ONLY, choose a bounded fraction of the current position.",
            "criteria": {"0.25": None, "0.50": None},
        },
    }


def build_state(snapshot: Mapping[str, Any], *, generated_at: float | None = None) -> dict[str, Any]:
    """Whitelist the state sent to TypeSafe; never include credentials or prompts."""
    now = time.time() if generated_at is None else float(generated_at)
    source = snapshot if isinstance(snapshot, Mapping) else {}
    return {
        "schema": "astra.fast_decision.v1",
        "generated_at": now,
        "snapshot": {
            "market": source.get("market") or source.get("factors") or {},
            "account": source.get("account") or {},
            "positions": source.get("positions") or [],
            "pending_orders": source.get("pending_orders") or [],
            "risk": source.get("risk") or {},
            "protection": source.get("protection") or {},
            "stale_after": source.get("stale_after"),
        },
    }


def parse_decision(response: Mapping[str, Any], *, received_at: float | None = None) -> dict[str, Any]:
    answers = response.get("answers") if isinstance(response, Mapping) else None
    answers = answers if isinstance(answers, Mapping) else {}
    action = normalize_action(answers.get("protective_action", "HOLD"))
    fraction_raw = _answer_value(answers.get("reduce_fraction"))
    try:
        reduce_fraction = float(fraction_raw)
    except (TypeError, ValueError):
        reduce_fraction = 0.0
    return {
        "schema": "astra.fast_decision.v1",
        "action": action,
        "reduce_fraction": reduce_fraction,
        "reason": str(_answer_value(answers.get("reason") or answers.get("protection_reason") or ""))[:500],
        "received_at": time.time() if received_at is None else float(received_at),
        "model": str(response.get("model") or ""),
        "usage": dict(response.get("usage") or {}) if isinstance(response.get("usage"), Mapping) else {},
    }


def validate_decision(decision: Mapping[str, Any], snapshot: Mapping[str, Any], *, now: float | None = None, ttl_seconds: float = 45.0) -> tuple[bool, str]:
    current = time.time() if now is None else float(now)
    action = normalize_action(decision.get("action"))
    if action not in ALLOWED_ACTIONS:
        return False, "action_not_allowed"
    try:
        received = float(decision.get("received_at", 0))
    except (TypeError, ValueError):
        return False, "decision_timestamp_invalid"
    if received <= 0 or current - received > max(1.0, float(ttl_seconds)):
        return False, "decision_stale"
    if action == "TIGHTEN_PROTECTION":
        price = (snapshot.get("tighten_stop_price") if isinstance(snapshot, Mapping) else None)
        if not finite_positive(price):
            return False, "tighten_price_missing"
    if action == "REDUCE_ONLY":
        try:
            fraction = float(decision.get("reduce_fraction", 0) or 0)
        except (TypeError, ValueError):
            return False, "reduce_fraction_invalid"
        if not math.isfinite(fraction) or fraction <= 0 or fraction > 0.5:
            return False, "reduce_fraction_out_of_bounds"
    return True, "ok"
