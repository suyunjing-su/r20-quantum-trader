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
    from astra_backend.equity_bands import save_bands, validate_band_targets
    try:
        domain_key = str(domain or "").strip().lower()
        validate_band_targets(domain_key, payload.bands)
        bands = save_bands(domain_key, payload.bands)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("equity_bands.update", "success", {"actor": actor["username"], "domain": domain, "count": len(bands)})
    return {"domain": domain, "bands": bands}
