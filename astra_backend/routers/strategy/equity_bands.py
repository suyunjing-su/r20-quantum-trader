"""Independent equity-band administration endpoints."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Header, HTTPException

from astra_backend.audit import record as audit_record
from astra_backend.dependencies import require_admin_header, require_superadmin
from astra_backend.schemas import EquityBandsUpdateRequest

router = APIRouter(tags=["strategy"])


@router.get("/api/v1/admin/equity-bands/{domain}")
def get_equity_bands(domain: str, x_astra_session: str | None = Header(default=None, alias="X-Astra-Session")) -> dict[str, Any]:
    require_admin_header(x_astra_session=x_astra_session)
    from astra_backend.equity_bands import list_bands
    try:
        return {"domain": domain, "bands": list_bands(domain)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/v1/admin/equity-bands/{domain}")
def put_equity_bands(domain: str, payload: EquityBandsUpdateRequest, x_astra_session: str | None = Header(default=None, alias="X-Astra-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_astra_session)
    from astra_backend.equity_bands import save_bands
    try:
        domain_key = str(domain or "").strip().lower()
        if domain_key == "council":
            from astra_backend.council_manager import list_council_profiles
            targets = {str(row.get("id")) for row in list_council_profiles()}
        elif domain_key == "prompt":
            from scripts.prompt_library import all_profiles
            targets = {str(row.get("id")) for row in all_profiles()}
        elif domain_key == "risk":
            from astra_backend import risk_config
            targets = {str(row.get("id")) for row in risk_config.SUITES}
            targets.update(str(row.get("id")) for row in risk_config.custom_suites())
        else:
            raise ValueError(f"未知资金区间域：{domain}")
        invalid = sorted({str(row.get("target_id") or "") for row in payload.bands} - targets)
        if invalid:
            raise ValueError(f"资金区间引用了不存在的方案：{', '.join(invalid)}")
        bands = save_bands(domain_key, payload.bands)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("equity_bands.update", "success", {"actor": actor["username"], "domain": domain, "count": len(bands)})
    return {"domain": domain, "bands": bands}
