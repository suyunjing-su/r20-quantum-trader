"""Read-only venue evidence adapters for durable entry-claim recovery.

All endpoints used here are GET/account-read operations.  The worker never
cancels or submits an order.  Negative query coverage is intentionally marked
incomplete when an exchange retention window or page cap can hide a match.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


_OKX_HISTORY_LIMIT = 100
_OKX_FILL_LIMIT = 100
_BINANCE_HISTORY_LIMIT = 1000
_BINANCE_FILL_LIMIT = 1000
_GATE_PAGE_SIZE = 100
_GATE_MAX_PAGES = 20


def _str(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _side(row: Mapping[str, Any], *, is_fill: bool = False) -> str:
    side = _str(row, "side", "order_side", "orderSide").lower()
    if side in {"buy", "sell"}:
        return side
    buyer = row.get("buyer")
    if buyer is not None:
        return "buy" if bool(buyer) else "sell"
    signed = _num(row.get("size_signed", row.get("size", row.get("amount"))))
    return "buy" if signed > 0 else "sell" if signed < 0 else ""


def _normalize_order(row: Mapping[str, Any], *, venue: str, source: str) -> Dict[str, Any]:
    status = _str(row, "state", "status", "order_status", "orderStatus").lower()
    if status in {"finished", "finish"}:
        status = _str(row, "finish_as", "finishAs").lower() or status
    rejected = status == "rejected"
    ambiguous_terminal = status in {"failed", "aborted"}
    order_id = _str(row, "order_id", "orderId", "ordId", "id_string", "id")
    client_id = _str(
        row, "client_order_id", "clientOrderId", "clOrdId", "cl_ord_id", "text"
    )
    raw_accepted = row.get("accepted")
    if raw_accepted is None:
        raw_accepted = row.get("was_accepted")
    item = {
        "venue": venue,
        "source": source,
        "order_id": order_id,
        "client_order_id": client_id,
        "symbol": _str(row, "instId", "inst_id", "symbol", "contract"),
        "side": _side(row),
        "posSide": _str(row, "posSide", "pos_side", "position_side", "positionSide").lower(),
        "status": status,
        "executedQty": _str(
            row, "executedQty", "filled_qty", "filledQty", "cumExecQty",
            "accFillSz", "dealSize"
        ),
    }
    if isinstance(raw_accepted, bool):
        item["accepted"] = raw_accepted
    elif rejected:
        item["accepted"] = False
    elif not ambiguous_terminal:
        item["accepted"] = source in {"live", "historical"}
    if row.get("pre_acceptance_terminal") is True:
        item["pre_acceptance_terminal"] = True
    elif rejected and raw_accepted is not True:
        item["pre_acceptance_terminal"] = True
    return item


def _normalize_fill(row: Mapping[str, Any], *, venue: str) -> Dict[str, Any]:
    return {
        "venue": venue,
        "order_id": _str(row, "order_id", "orderId", "ordId"),
        "client_order_id": _str(
            row, "client_order_id", "clientOrderId", "clOrdId", "cl_ord_id", "text"
        ),
        "symbol": _str(row, "instId", "inst_id", "symbol", "contract"),
        "side": _side(row, is_fill=True),
        "posSide": _str(row, "posSide", "pos_side", "position_side", "positionSide").lower(),
        "qty": _str(row, "qty", "fill_qty", "fillSz", "size", "amount", "sz"),
    }


def _normalize_position(row: Mapping[str, Any], *, venue: str) -> Dict[str, Any]:
    symbol = _str(row, "instId", "inst_id", "symbol", "contract")
    signed = _num(row.get("size_signed", row.get("positionAmt", row.get("pos"))))
    pos_side = _str(row, "posSide", "pos_side", "position_side", "positionSide", "side").lower()
    if pos_side == "short":
        signed = -abs(signed)
    elif pos_side == "long":
        signed = abs(signed)
    return {
        "venue": venue,
        "symbol": symbol,
        "size_signed": signed,
        "posSide": pos_side,
    }


def _list(value: Any) -> List[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("venue evidence endpoint returned a non-list response")
    return [row for row in value if isinstance(row, Mapping)]


class VenueEntryEvidenceReader:
    """Fetch and normalize read-only order, fill, and position evidence."""

    def __init__(
        self,
        *,
        adapter_provider: Optional[Callable[..., Any]] = None,
        okx_rest: Any = None,
        now_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        self._adapter_provider = adapter_provider
        self._okx_rest = okx_rest
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def _adapter(self, venue: str, environment: str) -> Any:
        from astra_backend.close_intent import adapter_environment
        adapter_env = adapter_environment(venue, environment)
        if self._adapter_provider is not None:
            adapter = self._adapter_provider(venue, environment=environment)
        else:
            from astra_backend.exchanges.registry import get_adapter
            adapter = get_adapter(venue, environment=adapter_env)
        actual_env = str(getattr(adapter, "environment", "") or "").strip().lower()
        if actual_env and actual_env != str(adapter_env).lower():
            raise RuntimeError(
                f"{venue} adapter environment mismatch: expected {adapter_env}, got {actual_env}")
        if self._adapter_provider is None and not actual_env:
            raise RuntimeError(f"{venue} adapter environment is missing")
        return adapter

    def _okx(self) -> Any:
        if self._okx_rest is not None:
            return self._okx_rest
        from scripts import okx_rest
        return okx_rest

    def read_entry_evidence(self, claim: Mapping[str, Any]) -> Dict[str, Any]:
        venue = _str(claim, "venue").lower()
        environment = _str(claim, "environment").lower()
        inst_id = _str(claim, "instId", "inst_id")
        claimed_at_ms = int(_num(claim.get("claimed_at_ms")))
        now_ms = int(self._now_ms())
        if not venue or not environment or not inst_id or claimed_at_ms <= 0:
            raise ValueError("claim lacks venue, environment, instrument, or claimed_at_ms")
        if venue == "okx":
            return self._read_okx(inst_id, environment, claimed_at_ms, now_ms)
        if venue == "binance":
            return self._read_binance(claim, inst_id, environment, claimed_at_ms, now_ms)
        if venue == "gate":
            return self._read_gate(inst_id, environment, claimed_at_ms, now_ms)
        raise ValueError(f"unsupported entry-claim venue: {venue}")

    def _read_okx(self, inst_id: str, environment: str,
                  claimed_at_ms: int, now_ms: int) -> Dict[str, Any]:
        rest = self._okx()
        env = rest.current_environment()
        if str(getattr(env, "mode", "")).lower() != environment:
            raise RuntimeError("冻结 OKX 环境与 entry claim 不一致，拒绝跨环境对账")
        live_raw = _list(rest.pending_orders(inst_id=inst_id, inst_type="SWAP", env=env))
        history_raw = _list(rest.orders_history(
            inst_type="SWAP", inst_id=inst_id, begin=claimed_at_ms,
            end=now_ms, limit=_OKX_HISTORY_LIMIT, env=env))
        fills_raw = _list(rest.fills(
            inst_type="SWAP", inst_id=inst_id, begin=claimed_at_ms,
            end=now_ms, limit=_OKX_FILL_LIMIT, env=env))
        positions_raw = _list(rest.positions(inst_type="SWAP", inst_id=inst_id, env=env))
        age_ms = max(0, now_ms - claimed_at_ms)
        coverage = {
            "live_orders": len(live_raw) < _OKX_HISTORY_LIMIT,
            "historical_orders": age_ms <= 30 * 24 * 60 * 60 * 1000
                               and len(history_raw) < _OKX_HISTORY_LIMIT,
            "fills": age_ms <= 3 * 24 * 60 * 60 * 1000
                     and len(fills_raw) < _OKX_FILL_LIMIT,
            "positions": True,
        }
        return {
            "query_ok": True,
            "venue": "okx",
            "environment": environment,
            "complete": all(coverage.values()),
            "coverage": coverage,
            "live_orders": [_normalize_order(row, venue="okx", source="live")
                            for row in live_raw],
            "historical_orders": [_normalize_order(row, venue="okx", source="historical")
                                  for row in history_raw],
            "fills": [_normalize_fill(row, venue="okx") for row in fills_raw],
            "positions": [_normalize_position(row, venue="okx") for row in positions_raw],
        }

    def _read_binance(self, claim: Mapping[str, Any], inst_id: str,
                      environment: str, claimed_at_ms: int, now_ms: int) -> Dict[str, Any]:
        adapter = self._adapter("binance", environment)
        native = adapter.native_symbol(inst_id)
        live_raw = _list(adapter.open_orders(native))
        # USD-M allOrders accepts a maximum seven-day interval and no longer
        # retains unfilled canceled/expired records beyond three days.  Keep
        # older claims unresolved even when the bounded query returns no rows.
        age_ms = max(0, now_ms - claimed_at_ms)
        query_start = max(claimed_at_ms, now_ms - 6 * 24 * 60 * 60 * 1000)
        history_raw = _list(adapter.fetch_order_history(
            native, start_time=query_start, end_time=now_ms,
            limit=_BINANCE_HISTORY_LIMIT))
        fills_raw = _list(adapter.fetch_account_trades(
            native, start_time=claimed_at_ms, end_time=now_ms,
            limit=_BINANCE_FILL_LIMIT))
        positions_raw = _list(adapter.positions())
        coverage = {
            "live_orders": len(live_raw) < _BINANCE_HISTORY_LIMIT,
            "historical_orders": age_ms <= 3 * 24 * 60 * 60 * 1000
                               and len(history_raw) < _BINANCE_HISTORY_LIMIT,
            "fills": age_ms <= 3 * 24 * 60 * 60 * 1000
                     and len(fills_raw) < _BINANCE_FILL_LIMIT,
            "positions": True,
        }
        return {
            "query_ok": True,
            "venue": "binance",
            "environment": environment,
            "complete": all(coverage.values()),
            "coverage": coverage,
            "live_orders": [_normalize_order(row, venue="binance", source="live")
                            for row in live_raw],
            "historical_orders": [_normalize_order(row, venue="binance", source="historical")
                                  for row in history_raw],
            "fills": [_normalize_fill(row, venue="binance") for row in fills_raw],
            "positions": [_normalize_position(row, venue="binance") for row in positions_raw],
        }

    @staticmethod
    def _gate_pages(method: Callable[..., Sequence[Mapping[str, Any]]],
                    inst_id: str, from_s: int, to_s: int
                    ) -> Tuple[List[Mapping[str, Any]], bool]:
        rows: List[Mapping[str, Any]] = []
        for page in range(_GATE_MAX_PAGES):
            batch = _list(method(inst_id, from_s=from_s, to_s=to_s,
                                 limit=_GATE_PAGE_SIZE, offset=page * _GATE_PAGE_SIZE))
            rows.extend(batch)
            if len(batch) < _GATE_PAGE_SIZE:
                return rows, True
        return rows, False

    def _read_gate(self, inst_id: str, environment: str,
                   claimed_at_ms: int, now_ms: int) -> Dict[str, Any]:
        adapter = self._adapter("gate", environment)
        live_raw = _list(adapter.list_open_orders(inst_id))
        from_s = claimed_at_ms // 1000
        to_s = now_ms // 1000
        history_raw, history_complete = self._gate_pages(
            adapter.fetch_order_history_timerange, inst_id, from_s, to_s)
        fills_raw, fills_complete = self._gate_pages(
            adapter.fetch_personal_trades_timerange, inst_id, from_s, to_s)
        positions_raw = _list(adapter.positions())
        coverage = {
            "live_orders": len(live_raw) < _GATE_PAGE_SIZE,
            "historical_orders": history_complete,
            "fills": fills_complete,
            "positions": True,
        }
        return {
            "query_ok": True,
            "venue": "gate",
            "environment": environment,
            "complete": all(coverage.values()),
            "coverage": coverage,
            "live_orders": [_normalize_order(row, venue="gate", source="live")
                            for row in live_raw],
            "historical_orders": [_normalize_order(row, venue="gate", source="historical")
                                  for row in history_raw],
            "fills": [_normalize_fill(row, venue="gate") for row in fills_raw],
            "positions": [_normalize_position(row, venue="gate") for row in positions_raw],
        }


def reconcile_unknown_entry_claims(*, path: Optional[str] = None,
                                   now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Run one read-only evidence reconciliation pass for durable unknown claims."""
    from scripts.trader.entry_claim_reconciliation import reconcile_open_entry_claims

    return reconcile_open_entry_claims(
        evidence_reader=VenueEntryEvidenceReader(), path=path, now_ms=now_ms)


__all__ = ["VenueEntryEvidenceReader", "reconcile_unknown_entry_claims"]
