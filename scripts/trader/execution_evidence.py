"""Durable execution-evidence sidecar for Binance entry lifecycle records."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict


def persist_venue_execution_evidence(evidence: Dict[str, Any]) -> bool:
    """Persist observed entry evidence keyed by the exchange order id.

    This is best-effort: an evidence-write failure must never undo an accepted order.
    Missing measurements stay null and are never synthesized as zero.
    """
    evidence_id = evidence.get("entry_order_id") or evidence.get("order_id")
    if not isinstance(evidence, dict) or not evidence_id:
        return False
    root = Path(os.environ.get("ASTRA_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
    path = root / "venue_execution_evidence.json"
    try:
        from astra_backend.file_locks import file_lock
        root.mkdir(parents=True, exist_ok=True)
        with file_lock(str(path)):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    rows = json.load(handle)
                if not isinstance(rows, dict):
                    rows = {}
            except (OSError, ValueError):
                rows = {}
            rows[str(evidence_id)] = evidence
            fd, tmp = tempfile.mkstemp(prefix=".binance-evidence-", suffix=".tmp", dir=str(root))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(rows, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return True
    except Exception as exc:
        print(f"[binance execution evidence] warn persist failed: {exc}")
        return False


def load_venue_execution_evidence(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            rows = json.load(handle)
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def match_timed_execution_evidence(rows: Dict[str, Any], *, venue: str, asset: str,
                                   position_side: str, evidence_type: str,
                                   target_time_ms: int, max_delta_ms: int = 20 * 60 * 1000) -> Dict[str, Any]:
    """Return a unique nearest lifecycle snapshot; ambiguous matches stay unobserved."""
    candidates = []
    for row in (rows or {}).values():
        if not isinstance(row, dict):
            continue
        if (str(row.get("venue") or "").lower() != str(venue).lower()
                or str(row.get("asset") or "").upper() != str(asset).upper()
                or str(row.get("position_side") or "").lower() != str(position_side).lower()
                or str(row.get("evidence_type") or "").lower() != str(evidence_type).lower()):
            continue
        try:
            delta = abs(int(row.get("observed_at_ms") or 0) - int(target_time_ms))
        except (TypeError, ValueError):
            continue
        if delta <= max_delta_ms:
            candidates.append((delta, row))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[0])
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return {}
    return candidates[0][1]


def measure_fill_vwap(trades: list) -> Dict[str, Any]:
    """Aggregate actual fills without fabricating absent data."""
    total_qty = total_quote = 0.0
    for trade in trades or []:
        try:
            qty = float(trade.get("qty") or 0)
            price = float(trade.get("price") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if qty > 0 and price > 0:
            total_qty += qty
            total_quote += qty * price
    if total_qty <= 0:
        return {"filled_qty": None, "vwap": None, "status": "UNOBSERVED"}
    return {"filled_qty": total_qty, "vwap": total_quote / total_qty, "status": "OBSERVED"}


def directional_slippage_bps(side: str, reference_price: Any, vwap: Any) -> float | None:
    """Positive means adverse execution; absent/invalid references remain null."""
    try:
        ref, actual = float(reference_price), float(vwap)
    except (TypeError, ValueError):
        return None
    if ref <= 0 or actual <= 0:
        return None
    sign = 1.0 if str(side).lower() in ("buy", "long", "buy_long") else -1.0
    return (actual - ref) / ref * 10000 * sign
