"""Admin controls and diagnostics for the independent Fast Decision guardian."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Body, Header, HTTPException

from astra_backend.audit import record as audit_record
from astra_backend.dependencies import require_admin_header, require_superadmin
from astra_backend.schemas import FastDecisionConfigUpdateRequest
from astra_backend.fast_decision_service import evaluate, entry_block
from astra_backend.fast_decision_store import config_status, load_config, save_config
from astra_backend.llm.system_one import SYSTEM_ONE_CAPABILITY, is_system_one_format
from astra_backend.llm_manager import load_llm_config, validate_model_runtime

router = APIRouter(tags=["fast-decision"])


def _validate_enabled_config(cfg: dict[str, Any]) -> None:
    if not cfg.get("enabled"):
        return
    pid, mid = str(cfg.get("provider_id") or ""), str(cfg.get("model_id") or "")
    if not pid or not mid:
        raise HTTPException(status_code=400, detail="启用 Fast Decision 前必须选择 System One 供应商与模型")
    raw = load_llm_config(mask_keys=False)
    provider = next((p for p in raw.get("providers", []) if str(p.get("id")) == pid), None)
    model = next((m for m in raw.get("models", []) if str(m.get("id")) == mid), None)
    if not provider or not model or str(model.get("provider_id")) != pid:
        raise HTTPException(status_code=400, detail="Fast Decision 模型不属于所选供应商")
    if not is_system_one_format(provider.get("api_format")) or not is_system_one_format(model.get("api_format")):
        raise HTTPException(status_code=400, detail="Fast Decision 只能绑定 TypeSafe System One 模型")
    caps = set(model.get("capabilities") or [])
    if SYSTEM_ONE_CAPABILITY not in caps and "system_one" not in caps:
        raise HTTPException(status_code=400, detail="所选模型未声明 structured_decision 能力")
    try:
        validate_model_runtime(mid, required_capability=SYSTEM_ONE_CAPABILITY)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _runtime_status(cfg: dict[str, Any]) -> dict[str, Any]:
    error = ""
    if cfg.get("enabled"):
        try:
            _validate_enabled_config(cfg)
        except HTTPException as exc:
            error = str(exc.detail)
    return {**config_status(cfg, runtime_error=error), "entry_block": entry_block()}


@router.get("/api/v1/admin/fast-decision/config")
def admin_get_fast_decision_config(x_astra_session: str | None = Header(default=None, alias="X-Astra-Session")) -> dict[str, Any]:
    require_admin_header(x_astra_session=x_astra_session)
    cfg = load_config()
    return {"config": cfg, "status": _runtime_status(cfg)}


@router.put("/api/v1/admin/fast-decision/config")
def admin_put_fast_decision_config(payload: FastDecisionConfigUpdateRequest, x_astra_session: str | None = Header(default=None, alias="X-Astra-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_astra_session)
    candidate = payload.model_dump()
    _validate_enabled_config(candidate)
    cfg = save_config(candidate)
    _validate_enabled_config(cfg)
    audit_record("fast_decision.config.update", "success", {
        "actor": actor["username"], "enabled": bool(cfg.get("enabled")),
        "provider_id": cfg.get("provider_id"), "model_id": cfg.get("model_id"),
    })
    return {"config": cfg, "status": _runtime_status(cfg)}


@router.get("/api/v1/admin/fast-decision/status")
def admin_get_fast_decision_status(x_astra_session: str | None = Header(default=None, alias="X-Astra-Session")) -> dict[str, Any]:
    require_admin_header(x_astra_session=x_astra_session)
    cfg = load_config()
    return _runtime_status(cfg)


@router.post("/api/v1/admin/fast-decision/evaluate")
def admin_evaluate_fast_decision(snapshot: dict[str, Any] = Body(default_factory=dict), x_astra_session: str | None = Header(default=None, alias="X-Astra-Session")) -> dict[str, Any]:
    require_admin_header(x_astra_session=x_astra_session)
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "action": "HOLD", "failure_safe": True, "error": "fast_decision_disabled"}
    _validate_enabled_config(cfg)
    runtime = validate_model_runtime(str(cfg["model_id"]), required_capability=SYSTEM_ONE_CAPABILITY)
    runtime.update({"timeout_seconds": cfg.get("timeout_seconds"), "decision_ttl_seconds": cfg.get("decision_ttl_seconds")})
    result = evaluate(snapshot, runtime)
    audit_record("fast_decision.evaluate", "success" if result.get("ok") else "failed", {"model_id": cfg.get("model_id"), "action": result.get("action")})
    return result
