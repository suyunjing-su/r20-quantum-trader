"""Fetch and validate native contract metadata before writing the master pool.

The master instrument pool keeps a small, serialisable snapshot of the native
metadata used by each selected venue.  It is deliberately fetched through the
existing adapters, so Binance/Gate sizing rules cannot be replaced with an
OKX-shaped placeholder.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable

from .exchanges.env_profiles import legacy_environment_for
from .exchanges.registry import get_adapter


def _positive(value: Any, field: str, venue: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{venue.upper()} 合约规格 {field} 无效: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{venue.upper()} 合约规格 {field} 必须为正数: {value!r}")
    return number


def _serialize_spec(spec: Any, venue: str) -> Dict[str, Any]:
    if spec is None:
        raise ValueError(f"{venue.upper()} 未返回合约原生规格")
    return {
        "venue": venue,
        "inst_id": str(spec.inst_id),
        "base": str(spec.base),
        "tick_size": _positive(spec.tick_size, "tick_size", venue),
        "step_size": _positive(spec.step_size, "step_size", venue),
        "ct_val": _positive(spec.ct_val, "ct_val", venue),
        "min_size": _positive(spec.min_size, "min_size", venue),
        "max_leverage": max(0.0, float(spec.max_leverage or 0.0)),
        "status": str(spec.status or "trading"),
        "quantity_unit": str(getattr(spec, "quantity_unit", "contracts") or "contracts"),
        "decimal_amount": bool(getattr(spec, "decimal_amount", False)),
        # Keep the raw exchange response for audit/debugging, but only JSON data.
        "raw": dict(spec.raw or {}),
    }


def _validate_native_spec(spec: Dict[str, Any], venue: str) -> None:
    raw = spec.get("raw") or {}
    status = str(spec.get("status") or "").lower()
    if venue == "binance":
        if status != "trading":
            raise ValueError(f"BINANCE 合约状态不可交易: {status or 'unknown'}")
        if raw.get("contractType") and raw.get("contractType") != "PERPETUAL":
            raise ValueError("BINANCE 不是 PERPETUAL 合约")
        if raw.get("quoteAsset") and raw.get("quoteAsset") != "USDT":
            raise ValueError("BINANCE 不是 USDT-M 合约")
        if raw.get("marginAsset") and raw.get("marginAsset") != "USDT":
            raise ValueError("BINANCE 保证金资产不是 USDT")
    elif venue == "okx" and status != "trading":
        raise ValueError(f"OKX 合约状态不可交易: {status or 'unknown'}")
    elif venue == "gate" and status != "trading":
        raise ValueError(f"GATE 合约状态不可交易: {status or 'unknown'}")


def fetch_selected_specs(inst_id: str, venues: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch exact native specs for every selected venue, or fail closed.

    ``fetch_instrument_spec`` uses Binance ``LOT_SIZE.stepSize`` and
    ``PRICE_FILTER.tickSize``, OKX ``lotSz/minSz/tickSz/ctVal``, and Gate
    ``order_size_min/order_price_round/quanto_multiplier``.  No precision field
    is used as a substitute for an order increment.
    """
    result: Dict[str, Dict[str, Any]] = {}
    for venue in sorted({str(v).strip().lower() for v in venues}):
        adapter = get_adapter(venue, environment=legacy_environment_for(venue))
        spec = adapter.fetch_instrument_spec(inst_id, refresh=True)
        serialized = _serialize_spec(spec, venue)
        _validate_native_spec(serialized, venue)
        result[venue] = serialized
    return result


def canonical_pool_metadata(specs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Derive legacy factor fields from a real selected venue spec.

    Multi-venue execution recalculates quantity with the target adapter.  These
    fields remain for the legacy factor package and are never fabricated.
    """
    if not specs:
        raise ValueError("没有可用的交易所原生规格")
    venue = sorted(specs)[0]
    spec = specs[venue]
    tick = float(spec["tick_size"])
    try:
        tick_text = format(Decimal(str(spec["tick_size"])), "f")
    except (InvalidOperation, ValueError):
        tick_text = str(spec["tick_size"])
    normalized_tick = tick_text.rstrip("0")
    precision = len(normalized_tick.split(".", 1)[1]) if "." in normalized_tick else 0
    return {
        "metadata_venue": venue,
        "base_sz": float(spec["min_size"]),
        "precision": precision,
        "ctVal": float(spec["ct_val"]),
        "tickSz": str(spec["tick_size"]),
        "minSz": str(spec["min_size"]),
    }


def add_native_specs(item: Dict[str, Any], specs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Return an item enriched with exact per-venue native contract metadata."""
    out = dict(item)
    out.update(canonical_pool_metadata(specs))
    out["venue_specs"] = specs
    return out


__all__ = ["fetch_selected_specs", "add_native_specs", "canonical_pool_metadata"]
