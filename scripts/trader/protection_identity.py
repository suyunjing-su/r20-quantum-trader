"""Typed, JSON-safe identities for exchange-side protective orders.

An exchange identity is the tuple (venue, resource family, identity kind,
value).  Values are intentionally never compared without their namespace:
Binance algoId/clientAlgoId, Gate id/id_string, and OKX algoId/algoClOrdId
are separate address spaces even when their string representations collide.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_IDENTITY_FIELDS = {
    "binance": (
        ("algo_id", "algoId", "algo_order"),
        ("client_algo_id", "clientAlgoId", "algo_order"),
    ),
    "gate": (
        ("id_string", "id_string", "price_order"),
        ("id", "id", "price_order"),
    ),
    "okx": (
        ("algo_id", "algoId", "algo_order"),
        ("algo_cl_ord_id", "algoClOrdId", "algo_order"),
    ),
}


def _containers(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    return [row] if raw is row else [row, raw]


def _first_value(containers: Sequence[Dict[str, Any]], *keys: str) -> Any:
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value not in (None, ""):
                return value
    return None


def _truth(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if value is True or str(value).strip().lower() in {"true", "1", "yes"}:
        return True
    if value is False or str(value).strip().lower() in {"false", "0", "no"}:
        return False
    return None


def _metadata(row: Dict[str, Any], *, venue: str,
              ownership_evidence: Optional[str],
              protection_kind: Optional[str]) -> Dict[str, Any]:
    containers = _containers(row)
    initial = row.get("initial") if isinstance(row.get("initial"), dict) else {}
    symbol = (_first_value(containers, "symbol", "contract", "inst_id", "instId")
              or initial.get("contract") or initial.get("symbol"))
    side = (_first_value(containers, "side", "order_side", "direction")
            or initial.get("direction"))
    position_side = _first_value(containers, "position_side", "positionSide", "posSide")
    quantity = (_first_value(containers, "quantity", "origQty", "qty", "size", "contracts", "sz")
                or initial.get("size"))
    full_close = _first_value(containers, "close", "is_close", "close_position", "closePosition")
    if full_close is None:
        full_close = initial.get("close", initial.get("is_close"))
    if full_close is None:
        full_close = _first_value(containers, "auto_size", "autoSize")
        if full_close not in (None, ""):
            full_close = True
    reduce_only = _first_value(containers, "reduce_only", "reduceOnly")
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    # Keep the exact exchange row as evidence. The copy prevents later adapter
    # mutation from changing a durable recovery snapshot in place.
    return {
        "venue": venue,
        "resource_family": "",
        "identity_kind": "",
        "identity_value": "",
        "symbol": "" if symbol is None else str(symbol),
        "raw_exchange": deepcopy(raw if raw else row),
        "ownership_evidence": ownership_evidence,
        "protection_kind": protection_kind,
        "side": "" if side is None else str(side),
        "position_side": "" if position_side is None else str(position_side),
        "quantity": None if quantity is None else str(quantity),
        "full_close": _truth(full_close),
        "reduce_only": _truth(reduce_only),
    }


def extract_protection_identities(
    row: Any,
    *,
    venue: Optional[str] = None,
    ownership_evidence: Optional[str] = None,
    protection_kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Extract every known typed identity from a venue protection row.

    Unknown venues and unsupported identity fields produce no identities. Gate
    float IDs are rejected because they may already have lost int64 precision;
    the string-safe id_string remains usable when present.
    """
    if not isinstance(row, dict):
        return []
    containers = _containers(row)
    venue_name = str(venue or _first_value(containers, "venue", "exchange") or "").strip().lower()
    if venue_name not in _IDENTITY_FIELDS:
        return []
    metadata = _metadata(row, venue=venue_name,
                         ownership_evidence=ownership_evidence,
                         protection_kind=protection_kind)
    result: List[Dict[str, Any]] = []
    for identity_kind, exchange_key, family in _IDENTITY_FIELDS[venue_name]:
        aliases = {
            "algo_id": ("algo_id", "algoId"),
            "client_algo_id": ("client_algo_id", "clientAlgoId"),
            "algo_cl_ord_id": ("algo_cl_ord_id", "algoClOrdId"),
            "id_string": ("id_string",),
            "id": ("id",),
        }[identity_kind]
        value = _first_value(containers, *aliases)
        if value in (None, ""):
            continue
        if venue_name == "gate" and identity_kind == "id" and isinstance(value, float):
            continue
        item = dict(metadata)
        item.update({
            "resource_family": family,
            "identity_kind": identity_kind,
            "identity_value": str(value).strip(),
        })
        if item["identity_value"]:
            result.append(item)
    return result


def normalize_typed_identity(value: Any) -> Optional[Dict[str, Any]]:
    """Validate and normalize a persisted typed identity without guessing."""
    if not isinstance(value, dict):
        return None
    venue = str(value.get("venue") or "").strip().lower()
    family = str(value.get("resource_family") or "").strip().lower()
    kind = str(value.get("identity_kind") or "").strip().lower()
    identity_value = value.get("identity_value")
    if identity_value in (None, "") or venue not in _IDENTITY_FIELDS:
        return None
    allowed = {(field_kind, resource) for field_kind, _key, resource in _IDENTITY_FIELDS[venue]}
    if (kind, family) not in allowed:
        return None
    if venue == "gate" and kind == "id" and isinstance(identity_value, float):
        return None
    out = dict(value)
    out.update({
        "venue": venue,
        "resource_family": family,
        "identity_kind": kind,
        "identity_value": str(identity_value).strip(),
    })
    if not out["identity_value"]:
        return None
    return out


def protection_identity_key(value: Any) -> Optional[Tuple[str, str, str, str]]:
    """Return an exact namespace-aware key, or None for malformed identity."""
    identity = normalize_typed_identity(value)
    if identity is None:
        return None
    return (identity["venue"], identity["resource_family"],
            identity["identity_kind"], identity["identity_value"])


def collect_protection_identities(
    rows: Optional[Iterable[Any]],
    *,
    venue: Optional[str],
    ownership_evidence: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Extract and de-duplicate typed identities while preserving order."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows or ():
        for identity in extract_protection_identities(
                row, venue=venue, ownership_evidence=ownership_evidence):
            key = protection_identity_key(identity)
            if key is not None and key not in seen:
                seen.add(key)
                out.append(identity)
    return out


def identities_from_snapshot(snapshot: Any, *, venue: Optional[str]) -> List[Dict[str, Any]]:
    """Read a persisted row snapshot into typed identities (legacy rows allowed)."""
    if not isinstance(snapshot, (list, tuple)):
        return []
    return collect_protection_identities(snapshot, venue=venue)


def identity_token_matches_row(token: str, row: Any, *, venue: str,
                               ownership_evidence: Optional[str] = None) -> List[Dict[str, Any]]:
    """Find typed identities whose exact value matches an opaque legacy token."""
    target = str(token or "").strip()
    if not target:
        return []
    return [identity for identity in extract_protection_identities(
        row, venue=venue, ownership_evidence=ownership_evidence)
        if identity["identity_value"] == target]
