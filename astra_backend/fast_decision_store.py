"""Durable Fast Decision configuration and action deduplication."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from astra_backend.file_locks import file_lock
from astra_backend.llm.util import _atomic_write_json

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CONFIG_FILE = DATA_DIR / "fast_decision_config.json"
ACTIONS_FILE = DATA_DIR / "fast_decision_actions.json"
ENTRY_BLOCK_FILE = DATA_DIR / "fast_decision_entry_block.json"
LATENCY_FILE = DATA_DIR / "fast_decision_latency.json"
MAX_LATENCY_SAMPLES = 500
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "provider_id": "",
    "model_id": "",
    "timeout_seconds": 8.0,
    "decision_ttl_seconds": 45.0,
    "action_dedup_seconds": 60.0,
}


def _read(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


def load_config() -> dict[str, Any]:
    raw = _read(CONFIG_FILE, {})
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, Mapping):
        cfg.update(raw)
    cfg["enabled"] = bool(cfg.get("enabled", False))
    for key, default in (("timeout_seconds", 8.0), ("decision_ttl_seconds", 45.0), ("action_dedup_seconds", 60.0)):
        try:
            cfg[key] = max(1.0, min(float(cfg.get(key, default)), 120.0 if key == "timeout_seconds" else 3600.0))
        except (TypeError, ValueError):
            cfg[key] = default
    cfg["provider_id"] = str(cfg.get("provider_id") or "").strip()
    cfg["model_id"] = str(cfg.get("model_id") or "").strip()
    return cfg


def save_config(values: Mapping[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    cfg.update({k: values[k] for k in DEFAULT_CONFIG if k in values})
    # Validate/coerce through the same rules used by load.
    with file_lock(CONFIG_FILE):
        _atomic_write_json(CONFIG_FILE, cfg)
    return load_config()


def config_status(config: Mapping[str, Any], *, runtime_error: str = "") -> dict[str, Any]:
    return {
        "enabled": bool(config.get("enabled")),
        "configured": bool(config.get("provider_id") and config.get("model_id")),
        "provider_id": str(config.get("provider_id") or ""),
        "model_id": str(config.get("model_id") or ""),
        "timeout_seconds": config.get("timeout_seconds"),
        "decision_ttl_seconds": config.get("decision_ttl_seconds"),
        "runtime_error": runtime_error,
        "safe_default": not bool(config.get("enabled")),
        "latency": latency_summary(),
    }


def claim_action(action_id: str, *, now: float | None = None, dedup_seconds: float = 60.0) -> bool:
    """Atomically claim an action key; false means a concurrent/repeated action wins."""
    key = str(action_id or "").strip()
    if not key:
        return False
    now_v = time.time() if now is None else float(now)
    with file_lock(ACTIONS_FILE):
        rows = _read(ACTIONS_FILE, {})
        rows = rows if isinstance(rows, dict) else {}
        fresh = {k: v for k, v in rows.items() if now_v - float(v or 0) < max(1.0, dedup_seconds)}
        if key in fresh:
            return False
        fresh[key] = now_v
        _atomic_write_json(ACTIONS_FILE, fresh)
    return True


def set_entry_block(*, expires_at: float, reason: str) -> None:
    with file_lock(ENTRY_BLOCK_FILE):
        _atomic_write_json(ENTRY_BLOCK_FILE, {"expires_at": float(expires_at), "reason": str(reason)[:500]})


def entry_block_status(*, now: float | None = None) -> dict[str, Any]:
    data = _read(ENTRY_BLOCK_FILE, {})
    now_v = time.time() if now is None else float(now)
    expires = float(data.get("expires_at", 0) or 0) if isinstance(data, dict) else 0.0
    return {"blocked": expires > now_v, "expires_at": expires, "reason": str(data.get("reason", ""))[:500] if isinstance(data, dict) else ""}


def record_latency_sample(sample: Mapping[str, Any]) -> None:
    """Persist bounded, secret-free Fast Decision latency samples."""
    clean: dict[str, Any] = {"recorded_at": time.time()}
    for key, value in sample.items():
        if key == "recorded_at":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number and number not in (float("inf"), float("-inf")):
            clean[str(key)] = round(number, 3)
    if len(clean) == 1:
        return
    with file_lock(LATENCY_FILE):
        rows = _read(LATENCY_FILE, [])
        rows = rows if isinstance(rows, list) else []
        rows.append(clean)
        _atomic_write_json(LATENCY_FILE, rows[-MAX_LATENCY_SAMPLES:])


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile + 0.999999)))
    return round(ordered[index], 3)


def latency_summary() -> dict[str, Any]:
    rows = _read(LATENCY_FILE, [])
    rows = rows if isinstance(rows, list) else []
    metrics: dict[str, Any] = {}
    keys = sorted({str(key) for row in rows if isinstance(row, dict) for key in row if key != "recorded_at"})
    for key in keys:
        values = []
        for row in rows:
            try:
                value = float(row.get(key))
            except (AttributeError, TypeError, ValueError):
                continue
            if value == value and value not in (float("inf"), float("-inf")):
                values.append(value)
        if values:
            metrics[key] = {
                "count": len(values),
                "p50_ms": _percentile(values, 0.50),
                "p95_ms": _percentile(values, 0.95),
                "p99_ms": _percentile(values, 0.99),
                "max_ms": round(max(values), 3),
            }
    return {"samples": len(rows), "metrics": metrics}
