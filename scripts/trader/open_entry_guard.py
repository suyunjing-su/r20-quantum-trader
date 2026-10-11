"""Persistent same-instrument entry de-duplication at the exchange boundary.

The cycle snapshot and the post-submit open-intent ledger are useful evidence, but
neither is a submit-time mutex.  This module owns the small, durable claim used by
the final entry submitter.  Claims are intentionally independent of strategy
memory: they prevent duplicate exposure caused by stale snapshots, retries, or
concurrent workers without deciding whether a trade thesis is good.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Tuple

from astra_backend.file_locks import file_lock

ROOT_DIR = Path(__file__).resolve().parents[2]
OPEN_ENTRY_CLAIMS_FILE = Path(
    os.getenv("ASTRA_OPEN_ENTRY_CLAIMS_FILE", str(ROOT_DIR / "data" / "open_entry_claims.json"))
)
DEFAULT_CLAIM_TTL_SECONDS = 6 * 60 * 60


def classify_entry_submission_exception(exc: BaseException) -> str:
    """Classify a failed entry write without guessing that it was not sent.

    Only failures that are demonstrably local or an explicit venue 4xx refusal
    may release a claim. Transport errors, timeouts, malformed/empty replies,
    and unknown adapter exceptions remain ``unknown_after_send`` so recovery
    can reconcile them before another order is attempted.
    """
    try:
        from astra_backend.exchanges.base import ExchangeCapabilityError
    except Exception:  # pragma: no cover - import fallback for isolated tools
        ExchangeCapabilityError = ()  # type: ignore[assignment]

    if isinstance(exc, (ValueError, TypeError, ExchangeCapabilityError)):
        return "rejected_before_send"

    status = getattr(exc, "status", getattr(exc, "status_code", 0))
    try:
        status = int(status or 0)
    except (TypeError, ValueError, OverflowError):
        status = 0
    # 408/425/429 are not explicit business refusals: a request may have
    # reached the venue and must be reconciled before retrying.
    if 400 <= status < 500 and status not in (408, 425, 429):
        return "rejected_before_send"

    code = str(getattr(exc, "code", getattr(exc, "label", "")) or "").lower()
    if code.startswith("local_") or code in {"invalid_param", "invalid_parameter"}:
        return "rejected_before_send"
    return "unknown_after_send"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError, TypeError):
        # A damaged claim file must not silently grant a second entry.  The
        # caller gets a fail-closed result from claim_open_entry instead.
        raise RuntimeError(f"开仓防重状态不可读取: {path}")
    if not isinstance(raw, list):
        raise RuntimeError(f"开仓防重状态格式非法: {path}")
    return [row for row in raw if isinstance(row, dict)]


def _atomic_write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _canonical_entry_inst_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        from astra_backend.exchanges.base import canonical_base
        base = canonical_base(text)
    except Exception:
        base = ""
    return str(base or text).strip().upper()


def _same_entry(row: dict[str, Any], *, environment: str, inst_id: str, side: str) -> bool:
    return (
        str(row.get("environment", "")).lower() == str(environment).lower()
        and _canonical_entry_inst_id(row.get("instId")) == _canonical_entry_inst_id(inst_id)
        and str(row.get("side", "")).lower() == str(side).lower()
    )


def _claim_ttl_ms(ttl_seconds: Optional[float] = None) -> int:
    try:
        return max(1_000, int(float(ttl_seconds if ttl_seconds is not None else
                                     os.getenv("ASTRA_OPEN_ENTRY_DEBOUNCE_SECONDS",
                                               DEFAULT_CLAIM_TTL_SECONDS)) * 1000))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_CLAIM_TTL_SECONDS * 1000


def claim_open_entry(*, environment: str, venue: str, inst_id: str, side: str,
                     ttl_seconds: Optional[float] = None,
                     path: Optional[os.PathLike[str] | str] = None,
                     now_ms: Optional[int] = None) -> Tuple[bool, str, str]:
    """Atomically claim an instrument/direction until submit outcome is known.

    The claim deliberately matches venue changes as well: a route change from
    OKX to Gate must not turn the same stale-snapshot signal into a second entry.
    ``status=pending`` protects the request while it is in flight; a successful
    submit leaves ``status=active`` until the short debounce TTL expires.
    """
    target = Path(path) if path is not None else OPEN_ENTRY_CLAIMS_FILE
    now = int(now_ms if now_ms is not None else _now_ms())
    ttl_ms = _claim_ttl_ms(ttl_seconds)

    try:
        with file_lock(target):
            rows = _read_rows(target)
            live = []
            for row in rows:
                status = str(row.get("status", "")).lower()
                if status in {"submitting", "dispatching", "unknown_after_send"}:
                    # Once a request may have crossed the network boundary, TTL
                    # cannot prove that it did not create exposure.
                    live.append(row)
                    continue
                try:
                    if int(row.get("expires_at_ms", 0) or 0) > now:
                        live.append(row)
                except (TypeError, ValueError, OverflowError):
                    # Unknown expiry is unsafe to reuse; keep it as a blocker.
                    live.append(row)
            for row in live:
                if _same_entry(row, environment=environment, inst_id=inst_id, side=side):
                    return False, "同标的同方向已有在途或近期开仓请求，等待持仓/挂单状态同步", ""

            if len(live) >= 500:
                return False, "开仓防重状态记录已满，安全拒绝下单", ""
            claim_id = uuid.uuid4().hex
            live.append({
                "claim_id": claim_id,
                "environment": str(environment),
                "venue": str(venue).lower(),
                "instId": str(inst_id),
                "side": str(side).lower(),
                "status": "pending",
                "claimed_at_ms": now,
                "expires_at_ms": now + ttl_ms,
            })
            _atomic_write(target, live)
            return True, "", claim_id
    except Exception as exc:
        return False, f"开仓防重状态不可用，安全拒绝下单: {exc}", ""


def validate_open_entry_claim(claim_id: str, *, environment: str, inst_id: str,
                              side: str,
                              path: Optional[os.PathLike[str] | str] = None,
                              now_ms: Optional[int] = None) -> bool:
    """Verify that a caller-supplied token owns the expected live namespace.

    Passing an arbitrary token must not disable the router's internal claim.
    This check is intentionally read-only and treats unreadable state as false.
    """
    if not claim_id:
        return False
    target = Path(path) if path is not None else OPEN_ENTRY_CLAIMS_FILE
    now = int(now_ms if now_ms is not None else _now_ms())
    try:
        with file_lock(target):
            for row in _read_rows(target):
                if str(row.get("claim_id", "")) != str(claim_id):
                    continue
                status = str(row.get("status", "")).lower()
                if not _same_entry(row, environment=environment,
                                   inst_id=inst_id, side=side):
                    return False
                if status == "submitting":
                    return True
                if status != "pending":
                    # Active and unknown claims block new claim creation, but
                    # neither may be replayed as authority to submit again.
                    return False
                try:
                    expires = int(row.get("expires_at_ms", 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    return False
                return expires > now
    except Exception:
        return False
    return False


def _transition_open_entry_claim(claim_id: str, *, status: str,
                                 allowed_from: set[str],
                                 path: Optional[os.PathLike[str] | str] = None,
                                 now_ms: Optional[int] = None,
                                 detail: str = "") -> bool:
    if not claim_id:
        return False
    target = Path(path) if path is not None else OPEN_ENTRY_CLAIMS_FILE
    now = int(now_ms if now_ms is not None else _now_ms())
    try:
        with file_lock(target):
            rows = _read_rows(target)
            for row in rows:
                if str(row.get("claim_id", "")) != str(claim_id):
                    continue
                previous = str(row.get("status", "pending")).lower()
                if previous not in allowed_from:
                    return False
                if status in {"submitting", "dispatching"} and previous == "pending":
                    try:
                        expires = int(row.get("expires_at_ms", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        return False
                    if expires <= now:
                        return False
                row["status"] = status
                row["updated_at_ms"] = now
                if status == "unknown_after_send":
                    # Expiry is not evidence that a possibly-sent order failed.
                    row.pop("expires_at_ms", None)
                if detail:
                    row["last_error"] = str(detail)[:300]
                _atomic_write(target, rows)
                return True
    except Exception:
        return False
    return False


def mark_open_entry_submitting(claim_id: str, *,
                               path: Optional[os.PathLike[str] | str] = None,
                               now_ms: Optional[int] = None) -> bool:
    """Persist the final facade transition before an entry request is routed."""
    return _transition_open_entry_claim(
        claim_id, status="submitting", allowed_from={"pending"},
        path=path, now_ms=now_ms,
    )


def begin_open_entry_submission(claim_id: str, *,
                                path: Optional[os.PathLike[str] | str] = None,
                                now_ms: Optional[int] = None) -> bool:
    """Consume a claim exactly once at the router's exchange-write boundary."""
    return _transition_open_entry_claim(
        claim_id, status="dispatching", allowed_from={"pending", "submitting"},
        path=path, now_ms=now_ms,
    )


def mark_open_entry_unknown(claim_id: str, detail: str = "", *,
                            path: Optional[os.PathLike[str] | str] = None,
                            now_ms: Optional[int] = None) -> bool:
    """Keep a possibly-sent entry blocked until exchange evidence resolves it."""
    return _transition_open_entry_claim(
        claim_id, status="unknown_after_send",
        allowed_from={"pending", "submitting", "dispatching", "unknown_after_send"},
        path=path, now_ms=now_ms, detail=detail,
    )


def commit_open_entry(claim_id: str, order_id: str = "", *,
                      path: Optional[os.PathLike[str] | str] = None,
                      now_ms: Optional[int] = None) -> bool:
    """Mark a confirmed accepted order active for the debounce window."""
    if not claim_id:
        return False
    target = Path(path) if path is not None else OPEN_ENTRY_CLAIMS_FILE
    now = int(now_ms if now_ms is not None else _now_ms())
    try:
        with file_lock(target):
            rows = _read_rows(target)
            changed = False
            for row in rows:
                if str(row.get("claim_id", "")) == str(claim_id):
                    if str(row.get("status", "pending")).lower() not in {
                            "pending", "submitting", "dispatching", "unknown_after_send"}:
                        return False
                    row["status"] = "active"
                    row["updated_at_ms"] = now
                    row["expires_at_ms"] = now + _claim_ttl_ms()
                    row.pop("last_error", None)
                    if order_id:
                        row["order_id"] = str(order_id)
                    changed = True
                    break
            if changed:
                _atomic_write(target, rows)
            return changed
    except Exception:
        # The claim itself remains in place if commit bookkeeping fails.
        return False


def release_open_entries_for(*, environment: str, inst_id: str, side: str,
                             path: Optional[os.PathLike[str] | str] = None) -> int:
    """Release accepted debounce claims after an exchange-confirmed position close.

    In-flight and unknown-outcome claims survive a position close: that close is
    not proof that their entry request was absent or rejected.
    """
    target = Path(path) if path is not None else OPEN_ENTRY_CLAIMS_FILE
    try:
        with file_lock(target):
            rows = _read_rows(target)
            kept = [row for row in rows
                    if not (_same_entry(row, environment=environment,
                                        inst_id=inst_id, side=side)
                            and str(row.get("status", "pending")).lower() == "active")]
            removed = len(rows) - len(kept)
            if removed:
                _atomic_write(target, kept)
            return removed
    except Exception:
        # Cleanup failure must not make a new entry look safe. Claims remain in place.
        return 0


def release_open_entry(claim_id: str, *, confirmed_not_sent: bool = False,
                       path: Optional[os.PathLike[str] | str] = None) -> bool:
    """Release only after explicit evidence the request was not accepted/sent."""
    if not claim_id or not confirmed_not_sent:
        return False
    target = Path(path) if path is not None else OPEN_ENTRY_CLAIMS_FILE
    try:
        with file_lock(target):
            rows = _read_rows(target)
            kept = [row for row in rows if str(row.get("claim_id", "")) != str(claim_id)]
            if len(kept) == len(rows):
                return False
            _atomic_write(target, kept)
            return True
    except Exception:
        # Never turn a submit failure into permission to retry.  Leaving the
        # claim behind is the safer recovery behavior.
        return False
