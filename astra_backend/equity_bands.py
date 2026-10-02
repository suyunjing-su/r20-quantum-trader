"""Independent settled-equity band mappings for council, prompts, and risk.

The resolver algorithm is shared, but each domain remains independently stored and
managed. Ranges use half-open intervals [min_equity, max_equity); max_equity=None
means no upper bound. Unknown equity never resolves a band.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from astra_backend.file_locks import file_lock

ROOT = Path(__file__).resolve().parents[1]
BANDS_FILE = ROOT / "data" / "equity_bands.json"
DOMAINS = ("council", "prompt", "risk")
VALID_MODES = ("split", "unified")
DEFAULT_MODE = "split"
MAX_BANDS = 100


def _empty() -> dict[str, list[dict[str, Any]]]:
    return {domain: [] for domain in DOMAINS}


def _empty_store() -> dict[str, Any]:
    return {
        **_empty(),
        "modes": {domain: DEFAULT_MODE for domain in DOMAINS},
        "unified_targets": {domain: "" for domain in DOMAINS},
    }


def _read() -> dict[str, Any]:
    try:
        raw = json.loads(BANDS_FILE.read_text(encoding="utf-8")) if BANDS_FILE.exists() else {}
    except (OSError, json.JSONDecodeError, TypeError):
        raw = {}
    out = _empty_store()
    if isinstance(raw, dict):
        for domain in DOMAINS:
            rows = raw.get(domain)
            if isinstance(rows, list):
                out[domain] = [dict(row) for row in rows if isinstance(row, dict)]
        modes = raw.get("modes")
        if isinstance(modes, dict):
            for domain in DOMAINS:
                mode = str(modes.get(domain) or DEFAULT_MODE).strip().lower()
                if mode in VALID_MODES:
                    out["modes"][domain] = mode
        targets = raw.get("unified_targets")
        if isinstance(targets, dict):
            for domain in DOMAINS:
                out["unified_targets"][domain] = str(targets.get(domain) or "").strip()
    return out


def _write(data: dict[str, Any]) -> None:
    BANDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".equity-bands-", suffix=".tmp", dir=BANDS_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(path, BANDS_FILE)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _domain(domain: str) -> str:
    key = str(domain or "").strip().lower()
    if key not in DOMAINS:
        raise ValueError(f"未知资金区间域：{domain}")
    return key


def _number(value: Any, *, name: str, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"{name} 必须是有限数字")
    return result


def validate_bands(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) > MAX_BANDS:
        raise ValueError(f"资金区间数量必须在 0 到 {MAX_BANDS} 条之间")
    normalized: list[dict[str, Any]] = []
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("资金区间必须是对象")
        band_id = str(row.get("id") or uuid.uuid4().hex).strip()
        if not band_id or band_id in ids or len(band_id) > 80:
            raise ValueError("资金区间 id 缺失、重复或过长")
        target_id = str(row.get("target_id") or "").strip()
        if not target_id or len(target_id) > 120:
            raise ValueError("资金区间必须指定 target_id")
        minimum = _number(row.get("min_equity", 0), name="min_equity")
        maximum = _number(row.get("max_equity"), name="max_equity", allow_none=True)
        if minimum is None or minimum < 0:
            raise ValueError("min_equity 必须大于等于 0")
        if maximum is not None and maximum <= minimum:
            raise ValueError("max_equity 必须大于 min_equity")
        normalized.append({
            "id": band_id,
            "min_equity": minimum,
            "max_equity": maximum,
            "target_id": target_id,
            "enabled": bool(row.get("enabled", True)),
            "priority": int(row.get("priority", 0) or 0),
        })
        ids.add(band_id)

    enabled = sorted((row for row in normalized if row["enabled"]),
                     key=lambda row: (row["min_equity"], row["max_equity"] is None,
                                      row["max_equity"] or float("inf"), row["priority"], row["id"]))
    for left, right in zip(enabled, enabled[1:]):
        left_max = left["max_equity"]
        if left_max is None or right["min_equity"] < left_max:
            raise ValueError(f"资金区间重叠：{left['id']} 与 {right['id']}")
    return normalized


def list_bands(domain: str) -> list[dict[str, Any]]:
    key = _domain(domain)
    with file_lock(BANDS_FILE):
        return _read()[key]


def domain_settings(domain: str) -> dict[str, str]:
    """Return the selection mode without exposing the storage layout."""
    key = _domain(domain)
    with file_lock(BANDS_FILE):
        store = _read()
        return {
            "mode": str(store["modes"].get(key) or DEFAULT_MODE),
            "unified_target_id": str(store["unified_targets"].get(key) or ""),
        }


def save_domain_config(
    domain: str,
    rows: list[dict[str, Any]] | None = None,
    *,
    mode: str | None = None,
    unified_target_id: str | None = None,
) -> dict[str, Any]:
    """Persist range rows and/or the target-selection mode atomically.

    Split mode keeps the existing half-open equity ranges. Unified mode selects
    exactly one target and leaves split ranges intact as a dormant configuration,
    so switching modes never destroys an operator's previous range design.
    """
    key = _domain(domain)
    normalized = validate_bands(rows) if rows is not None else None
    clean_mode = str(mode).strip().lower() if mode is not None else None
    if clean_mode is not None and clean_mode not in VALID_MODES:
        raise ValueError(f"资金区间模式必须是：{', '.join(VALID_MODES)}")
    clean_target = str(unified_target_id or "").strip() if unified_target_id is not None else None
    with file_lock(BANDS_FILE):
        store = _read()
        selected_mode = clean_mode or str(store["modes"].get(key) or DEFAULT_MODE)
        selected_target = clean_target if clean_target is not None else str(store["unified_targets"].get(key) or "")
        if selected_mode == "unified" and not selected_target:
            raise ValueError("统一模式必须选择一个方案")
        if normalized is not None:
            store[key] = normalized
        store["modes"][key] = selected_mode
        store["unified_targets"][key] = selected_target
        _write(store)
        return {
            "domain": key,
            "bands": list(store[key]),
            "mode": selected_mode,
            "unified_target_id": selected_target,
        }


def save_bands(domain: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = save_domain_config(domain, rows)
    return result["bands"]


def _target_ids(domain: str) -> set[str]:
    key = _domain(domain)
    if key == "council":
        from astra_backend.council_manager import list_council_profiles
        return {str(row.get("id")) for row in list_council_profiles()}
    if key == "prompt":
        from scripts.prompt_library import all_profiles
        return {str(row.get("id")) for row in all_profiles()}
    from astra_backend import risk_config
    targets = {str(row.get("id")) for row in risk_config.SUITES}
    targets.update(str(row.get("id")) for row in risk_config.custom_suites())
    return targets


def validate_band_target(domain: str, target_id: str) -> None:
    key = _domain(domain)
    target = str(target_id or "").strip()
    if not target or target not in _target_ids(key):
        raise ValueError(f"资金区间引用了不存在的方案：{target or '(空)'}")


def validate_band_targets(domain: str, rows: list[dict[str, Any]]) -> None:
    """Reject dangling band references before persisting a domain mapping."""
    targets = _target_ids(domain)
    invalid = sorted({str(row.get("target_id") or "") for row in rows} - targets)
    if invalid:
        raise ValueError(f"资金区间引用了不存在的方案：{', '.join(invalid)}")


def is_target_referenced(domain: str, target_id: str) -> bool:
    """Whether a target is referenced by active or dormant selection settings."""
    key = _domain(domain)
    target = str(target_id or "").strip()
    with file_lock(BANDS_FILE):
        store = _read()
        if str(store["unified_targets"].get(key) or "") == target:
            return True
        return any(str(row.get("target_id") or "") == target for row in store[key])


def resolve_band(domain: str, settled_equity: Any) -> dict[str, Any] | None:
    key = _domain(domain)
    settings = domain_settings(key)
    if settings["mode"] == "unified":
        target_id = settings["unified_target_id"]
        if not target_id:
            return None
        return {
            "id": f"unified-{key}",
            "min_equity": 0.0,
            "max_equity": None,
            "target_id": target_id,
            "enabled": True,
            "priority": 0,
            "mode": "unified",
        }
    if settled_equity is None:
        return None
    try:
        equity = _number(settled_equity, name="settled_equity")
    except ValueError:
        return None
    if equity is None or equity < 0:
        return None
    for row in list_bands(key):
        if not row.get("enabled", True):
            continue
        minimum = float(row.get("min_equity", 0))
        maximum = row.get("max_equity")
        if equity >= minimum and (maximum is None or equity < float(maximum)):
            return dict(row)
    return None


def resolve_council_profile(equity: Any) -> dict[str, Any] | None:
    return resolve_band("council", equity)


def resolve_prompt_profile(equity: Any) -> dict[str, Any] | None:
    return resolve_band("prompt", equity)


def resolve_risk_suite(equity: Any) -> dict[str, Any] | None:
    return resolve_band("risk", equity)
