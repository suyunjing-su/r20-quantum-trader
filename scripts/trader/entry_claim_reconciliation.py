"""Evidence-driven recovery for durable entry claims.

This module deliberately separates attribution decisions from exchange access.  A
venue reader supplies normalized, read-only evidence; :func:`decide_claim_evidence`
then applies the same fail-closed rules to every venue.  The decision layer never
cancels an order and never treats a missing live order, a position-only match, or
an incomplete query as proof that an ambiguous request was not sent.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence


UNKNOWN_ENTRY_STATUSES = frozenset({
    "pending",
    "submitting",
    "dispatching",
    "unknown_after_send",
})

_ACCEPTED_STATES = frozenset({
    "accepted",
    "closed",
    "done",
    "filled",
    "live",
    "new",
    "open",
    "partial",
    "partially_filled",
    "pending",
    "working",
})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _first(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _canonical_inst_id(value: Any) -> str:
    """Normalize the common spot/swap spellings without importing a venue SDK."""
    text = _text(value).upper()
    if not text:
        return ""
    text = text.replace("/", "-").replace("_", "-")
    text = re.sub(r"-SWAP$", "", text)
    compact = re.sub(r"[^A-Z0-9]", "", text)
    if compact.endswith("SWAP"):
        compact = compact[:-4]
    return compact


def _claim_client_id(claim: Mapping[str, Any]) -> str:
    return _first(claim, "client_order_id", "clientOrderId", "clOrdId", "correlation_id")


def _row_client_id(row: Mapping[str, Any]) -> str:
    return _first(
        row,
        "client_order_id",
        "clientOrderId",
        "clOrdId",
        "cl_ord_id",
        "text",
        "correlation_id",
    )


def _row_server_id(row: Mapping[str, Any]) -> str:
    return _first(row, "order_id", "orderId", "ordId", "id")


def _row_state(row: Mapping[str, Any]) -> str:
    return _lower(_first(row, "state", "status", "order_status", "orderStatus"))


def _expected_order_side(claim: Mapping[str, Any]) -> str:
    side = _lower(claim.get("side"))
    return {"long": "buy", "short": "sell"}.get(side, "")


def _row_matches_instrument_and_side(
    claim: Mapping[str, Any], row: Mapping[str, Any], *, position: bool = False
) -> bool:
    claim_venue = _lower(claim.get("venue"))
    row_venue = _lower(row.get("venue"))
    if claim_venue and row_venue and claim_venue != row_venue:
        return False
    if not position and (not claim_venue or not row_venue):
        return False

    claim_inst = _canonical_inst_id(claim.get("instId") or claim.get("inst_id"))
    row_inst = _canonical_inst_id(
        _first(row, "instId", "inst_id", "symbol", "contract", "instrument")
    )
    # Positive attribution requires the evidence row to identify the exact
    # instrument; a missing symbol is not made trustworthy by a matching size.
    if not claim_inst or not row_inst or claim_inst != row_inst:
        return False

    claim_side = _lower(claim.get("side"))
    row_pos_side = _lower(_first(row, "posSide", "position_side", "positionSide"))
    if row_pos_side in {"long", "short"} and claim_side in {"long", "short"}:
        if row_pos_side != claim_side:
            return False

    if not position:
        row_side = _lower(_first(row, "side", "order_side", "orderSide"))
        expected = _expected_order_side(claim)
        if expected and row_side != expected:
            return False
    return True


def _matching_order(claim: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    client_id = _claim_client_id(claim)
    if not client_id or _row_client_id(row) != client_id:
        return False
    return _row_matches_instrument_and_side(claim, row)


def _matching_fill(claim: Mapping[str, Any], fill: Mapping[str, Any], order_ids: set[str]) -> bool:
    if not _row_matches_instrument_and_side(claim, fill):
        return False
    client_id = _row_client_id(fill)
    if client_id and client_id == _claim_client_id(claim):
        return True
    fill_order_id = _row_server_id(fill)
    return bool(fill_order_id and fill_order_id in order_ids)


def _numeric(row: Mapping[str, Any], *keys: str) -> float:
    value = _first(row, *keys)
    if not value:
        return 0.0
    try:
        return abs(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _position_has_exposure(claim: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    if not _row_matches_instrument_and_side(claim, row, position=True):
        return False
    size = _numeric(row, "size_signed", "size", "pos", "positionAmt", "quantity", "qty")
    if size <= 1e-12:
        return False
    claim_side = _lower(claim.get("side"))
    row_pos_side = _lower(_first(row, "posSide", "position_side", "positionSide"))
    if row_pos_side in {"long", "short"}:
        return row_pos_side == claim_side
    try:
        signed = float(_first(row, "size_signed", "positionAmt", "pos") or 0)
    except (TypeError, ValueError, OverflowError):
        return True
    if claim_side == "long":
        return signed > 0
    if claim_side == "short":
        return signed < 0
    return True


def _filled_quantity(row: Mapping[str, Any]) -> float:
    return max(
        _numeric(
            row,
            "filled_qty",
            "filledQty",
            "executedQty",
            "cumExecQty",
            "dealSize",
            "fill_qty",
            "qty",
        ),
        0.0,
    )


def _definitely_not_accepted(row: Mapping[str, Any]) -> bool:
    state = _row_state(row)
    accepted = row.get("accepted")
    if accepted is None:
        accepted = row.get("was_accepted")
    if accepted is True:
        return False
    if state == "rejected":
        return True
    if state not in {"aborted", "canceled", "cancelled", "expired", "failed"}:
        return False
    # A canceled/expired order may first have been accepted and then canceled.
    # Release only when the venue reader gives explicit pre-acceptance evidence.
    if row.get("pre_acceptance_terminal") is True:
        return True
    return accepted is False


def _is_accepted(row: Mapping[str, Any], *, live: bool = False) -> bool:
    if _definitely_not_accepted(row):
        return False
    accepted = row.get("accepted")
    if accepted is None:
        accepted = row.get("was_accepted")
    if accepted is False:
        return False
    if live or accepted is True:
        return True
    state = _row_state(row)
    return state in _ACCEPTED_STATES or _filled_quantity(row) > 0


def _candidate_key(row: Mapping[str, Any]) -> str:
    server_id = _row_server_id(row)
    if server_id:
        return "order:" + server_id
    return "client:" + _row_client_id(row)


def _as_rows(evidence: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    value = evidence.get(key) or []
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _query_complete(evidence: Mapping[str, Any]) -> bool:
    if evidence.get("query_ok") is False or evidence.get("complete") is False:
        return False
    coverage = evidence.get("coverage")
    if not isinstance(coverage, Mapping):
        return False
    required = ("live_orders", "historical_orders", "fills", "positions")
    return all(coverage.get(key) is True for key in required)


def _evidence_summary(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    coverage = evidence.get("coverage")
    return {
        "query_ok": evidence.get("query_ok") is not False,
        "complete": _query_complete(evidence),
        "coverage": dict(coverage) if isinstance(coverage, Mapping) else {},
        "live_orders": len(_as_rows(evidence, "live_orders")),
        "historical_orders": len(_as_rows(evidence, "historical_orders")),
        "fills": len(_as_rows(evidence, "fills")),
        "positions": len(_as_rows(evidence, "positions")),
    }


def _decision(
    category: str,
    reason: str,
    *,
    order_id: str = "",
    evidence: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    action = {
        "promoted_to_active": "promote_to_active",
        "released_with_evidence": "release",
    }.get(category, "retain")
    result: Dict[str, Any] = {
        "action": action,
        "category": category,
        "reason": reason,
    }
    if order_id:
        result["order_id"] = order_id
    if evidence is not None:
        result["evidence"] = _evidence_summary(evidence)
    return result


def decide_claim_evidence(
    claim: Mapping[str, Any], evidence: Mapping[str, Any]
) -> Dict[str, Any]:
    """Return a fail-closed decision for one durable entry claim.

    ``live_orders``, ``historical_orders``, ``fills``, and ``positions`` lists, plus
    exact ``venue``/``environment`` scope and either ``complete=True`` or a
    four-axis ``coverage`` map.  Negative release decisions require complete
    coverage; positive decisions require a unique order/fill identity matching
    the persisted client ID, venue, environment, instrument, and side.
    """
    status = _lower(claim.get("status"))
    if status not in UNKNOWN_ENTRY_STATUSES:
        return _decision(
            "retained_unknown",
            f"claim status {status or 'missing'} is not a reconciliation candidate",
            evidence=evidence,
        )

    if evidence.get("query_ok") is False:
        return _decision(
            "query_failures",
            _text(evidence.get("error")) or "authoritative venue query failed",
            evidence=evidence,
        )

    client_id = _claim_client_id(claim)
    if not client_id:
        return _decision(
            "retained_unknown",
            "claim has no persisted client correlation ID; attribution is impossible",
            evidence=evidence,
        )

    claim_venue = _lower(claim.get("venue"))
    evidence_venue = _lower(evidence.get("venue"))
    if not claim_venue or not evidence_venue or claim_venue != evidence_venue:
        return _decision(
            "retained_unknown",
            "evidence venue scope is missing or does not match the claim",
            evidence=evidence,
        )
    claim_environment = _lower(claim.get("environment"))
    evidence_environment = _lower(evidence.get("environment"))
    if (not claim_environment or not evidence_environment
            or claim_environment != evidence_environment):
        return _decision(
            "retained_unknown",
            "evidence environment scope is missing or does not match the claim",
            evidence=evidence,
        )

    live = [row for row in _as_rows(evidence, "live_orders") if _matching_order(claim, row)]
    historical = [
        row for row in _as_rows(evidence, "historical_orders") if _matching_order(claim, row)
    ]
    fills = _as_rows(evidence, "fills")
    positions = _as_rows(evidence, "positions")

    candidates: Dict[str, Dict[str, Any]] = {}
    for row in live:
        key = _candidate_key(row)
        if key not in candidates:
            candidates[key] = {"rows": [], "live": False}
        candidates[key]["rows"].append(row)
        candidates[key]["live"] = True
    for row in historical:
        key = _candidate_key(row)
        if key not in candidates:
            candidates[key] = {"rows": [], "live": False}
        candidates[key]["rows"].append(row)

    # A fill carrying this exact client ID is itself an attributable identity,
    # even if the venue's order-history endpoint no longer returns the order.
    for fill in fills:
        if (_row_client_id(fill) != client_id
                or not _row_matches_instrument_and_side(claim, fill)):
            continue
        key = _candidate_key(fill)
        if key not in candidates:
            candidates[key] = {"rows": [], "live": False}
        candidates[key]["rows"].append(fill)

    candidate_fills: Dict[str, List[Mapping[str, Any]]] = {}
    for candidate_key, candidate in candidates.items():
        ids = {
            _row_server_id(row)
            for row in candidate["rows"]
            if _row_server_id(row)
        }
        candidate_fills[candidate_key] = [
            fill for fill in fills if _matching_fill(claim, fill, ids)
        ]

    relevant_positions = [row for row in positions if _position_has_exposure(claim, row)]

    if len(candidates) > 1:
        return _decision(
            "ambiguous",
            "multiple distinct exchange orders match the persisted client correlation ID",
            evidence=evidence,
        )

    if len(candidates) == 1:
        candidate_key, candidate = next(iter(candidates.items()))
        rows = candidate["rows"]
        has_fill = any(_filled_quantity(row) > 0 for row in candidate_fills[candidate_key])
        has_fill = has_fill or any(_filled_quantity(row) > 0 for row in rows)
        accepted = any(_is_accepted(row, live=candidate["live"]) for row in rows)
        rejected = all(_definitely_not_accepted(row) for row in rows)
        order_id = next((_row_server_id(row) for row in rows if _row_server_id(row)), "")
        if has_fill:
            return _decision(
                "promoted_to_active",
                "matching order has attributable execution evidence",
                order_id=order_id,
                evidence=evidence,
            )
        if candidate["live"] and accepted:
            return _decision(
                "promoted_to_active",
                "unique live order matches the persisted client correlation ID",
                order_id=order_id,
                evidence=evidence,
            )
        if accepted and not rejected:
            return _decision(
                "promoted_to_active",
                "unique accepted historical order matches the persisted client correlation ID",
                order_id=order_id,
                evidence=evidence,
            )
        if rejected:
            if relevant_positions:
                return _decision(
                    "retained_unknown",
                    "terminal rejection exists but a relevant position remains; absence of attribution is not proven",
                    evidence=evidence,
                )
            if not _query_complete(evidence):
                return _decision(
                    "retained_unknown",
                    "terminal rejection exists but evidence coverage is incomplete",
                    evidence=evidence,
                )
            return _decision(
                "released_with_evidence",
                "complete evidence shows explicit pre-acceptance rejection with no fill or position",
                order_id=order_id,
                evidence=evidence,
            )

    if relevant_positions:
        return _decision(
            "retained_unknown",
            "position evidence is not attributable to this request without a matching order identity",
            evidence=evidence,
        )

    return _decision(
        "retained_unknown",
        "no uniquely attributable order evidence was found; an empty live-order result is not proof of rejection",
        evidence=evidence,
    )


def _read_evidence(evidence_reader: Any, claim: Mapping[str, Any]) -> Mapping[str, Any]:
    if callable(evidence_reader):
        result = evidence_reader(claim)
    elif hasattr(evidence_reader, "read_entry_evidence"):
        result = evidence_reader.read_entry_evidence(claim)
    elif hasattr(evidence_reader, "read"):
        result = evidence_reader.read(claim)
    else:
        raise TypeError("evidence_reader must be callable or expose read_entry_evidence/read")
    if not isinstance(result, Mapping):
        raise TypeError("evidence_reader returned a non-mapping evidence result")
    return result


def reconcile_claim_snapshot(
    claims: Sequence[Mapping[str, Any]],
    *,
    evidence_reader: Any,
    persist: Optional[Callable[[Mapping[str, Any], Mapping[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """Evaluate an isolated claim snapshot with injected read-only evidence.

    Only unknown-after-send claims are queried here.  In particular, active
    submitters in other processes are not raced by a periodic negative query.
    """
    reports: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for claim in claims:
        status = _lower(claim.get("status"))
        if status != "unknown_after_send":
            continue
        claim_id = _text(claim.get("claim_id"))
        if not _claim_client_id(claim):
            evidence: Mapping[str, Any] = {"query_ok": True, "complete": False}
            result = decide_claim_evidence(claim, evidence)
        else:
            try:
                evidence = _read_evidence(evidence_reader, claim)
                result = decide_claim_evidence(claim, evidence)
            except Exception as exc:
                result = _decision("query_failures", f"evidence reader failed: {exc}")
                evidence = {"query_ok": False, "error": str(exc)}
        report = {
            "claim_id": claim_id,
            "venue": _lower(claim.get("venue")),
            "instId": _text(claim.get("instId")),
            "side": _lower(claim.get("side")),
            **result,
        }
        reports.append(report)
        category = str(result.get("category") or "retained_unknown")
        counts[category] = counts.get(category, 0) + 1
        if persist is not None:
            try:
                persist_result = persist(claim, result)
                persisted = persist_result is not False
            except Exception:
                persisted = False
            report["persisted"] = persisted
            if not persisted:
                counts["persistence_failures"] = counts.get("persistence_failures", 0) + 1
    return {
        "ok": not any(key in counts for key in ("query_failures", "persistence_failures")),
        "reports": reports,
        "counts": counts,
    }


def reconcile_open_entry_claims(
    *,
    evidence_reader: Any,
    path: Optional[str] = None,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Load durable unknown claims, inspect evidence, and persist decisions.

    The reader is injected because each exchange has distinct endpoints and
    history-retention guarantees.  This orchestration makes no exchange writes.
    """
    from scripts.trader.open_entry_guard import (
        apply_open_entry_reconciliation,
        read_open_entry_claims,
    )

    ok, claims, error = read_open_entry_claims(path=path)
    if not ok:
        return {
            "ok": False,
            "reports": [],
            "counts": {"query_failures": 1},
            "error": error,
        }
    return reconcile_claim_snapshot(
        claims,
        evidence_reader=evidence_reader,
        persist=lambda claim, result: apply_open_entry_reconciliation(
            str(claim.get("claim_id") or ""), result, path=path, now_ms=now_ms),
    )


__all__ = [
    "UNKNOWN_ENTRY_STATUSES",
    "decide_claim_evidence",
    "reconcile_claim_snapshot",
    "reconcile_open_entry_claims",
]
