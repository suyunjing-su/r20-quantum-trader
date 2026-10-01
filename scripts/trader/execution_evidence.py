"""Durable execution-evidence sidecar for Binance entry lifecycle records."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict


def persist_binance_execution_evidence(evidence: Dict[str, Any]) -> bool:
    """Persist observed entry evidence keyed by the exchange order id.

    This is best-effort: an evidence-write failure must never undo an accepted order.
    Missing measurements stay null and are never synthesized as zero.
    """
    if not isinstance(evidence, dict) or not evidence.get("entry_order_id"):
        return False
    root = Path(os.environ.get("ASTRA_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
    path = root / "binance_execution_evidence.json"
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
            rows[str(evidence["entry_order_id"])] = evidence
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


def load_binance_execution_evidence(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            rows = json.load(handle)
        return rows if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


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
