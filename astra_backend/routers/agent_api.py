"""Least-scope Agent API endpoints for exchange/routing configuration and telemetry."""
from __future__ import annotations

import os
import re
import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from astra_backend.audit import record as audit_record
from astra_backend.dependencies import (
    AGENT_SCOPE_EQUITY_BANDS_WRITE,
    agent_api_key_scopes,
    require_admin_header,
    require_superadmin,
)
from astra_backend.settings_store import update_env

AGENT_SCOPES = {AGENT_SCOPE_EQUITY_BANDS_WRITE}
VENUES = ("okx", "binance", "gate")
agent_router = APIRouter(tags=["agent-api"])


class AgentApiScopesUpdate(BaseModel):
    """Explicit administrator grant list for the single machine API key."""
    scopes: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_scopes(self):
        unknown = set(self.scopes) - AGENT_SCOPES
        if unknown:
            raise ValueError(f"未知 Agent API scope: {', '.join(sorted(unknown))}")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("Agent API scopes 不得重复")
        return self


@agent_router.get("/api/v1/admin/agent-api-key/scopes")
def get_agent_api_key_scopes() -> dict[str, Any]:
    """Read the non-secret scope grant; only an administrator may change it."""
    require_superadmin()
    return {
        "configured": bool(os.getenv("ASTRA_AGENT_API_KEY", "").strip()),
        "scopes": sorted(agent_api_key_scopes()),
        "available_scopes": sorted(AGENT_SCOPES),
    }


@agent_router.put("/api/v1/admin/agent-api-key/scopes")
def put_agent_api_key_scopes(payload: AgentApiScopesUpdate) -> dict[str, Any]:
    """Grant/revoke optional Agent capabilities without rotating the secret."""
    actor = require_superadmin()
    scopes = sorted(set(payload.scopes))
    update_env({"ASTRA_AGENT_API_KEY_SCOPES": ",".join(scopes)})
    audit_record("agent_api_key.scopes.update", "success", {
        "actor": actor.get("username", "admin"), "scopes": scopes,
    })
    return {"configured": bool(os.getenv("ASTRA_AGENT_API_KEY", "").strip()), "scopes": scopes}


@agent_router.get("/api/v1/admin/agent-api-key")
def get_agent_api_key_status() -> dict[str, bool]:
    """Expose key presence only; the secret itself is never readable after creation."""
    require_superadmin()
    return {"configured": bool(os.getenv("ASTRA_AGENT_API_KEY", "").strip())}


@agent_router.post("/api/v1/admin/agent-api-key")
def generate_agent_api_key() -> dict[str, Any]:
    """Generate/rotate the scoped Agent key and disclose it only in this response."""
    actor = require_superadmin()
    key = secrets.token_urlsafe(48)
    update_env({"ASTRA_AGENT_API_KEY": key})
    audit_record("agent_api_key.generate", "success", {"actor": actor.get("username", "admin")})
    return {"configured": True, "api_key": key, "shown_once": True}


@agent_router.delete("/api/v1/admin/agent-api-key")
def delete_agent_api_key() -> dict[str, bool]:
    """Revoke the current key persistently, including deployment environment fallbacks."""
    actor = require_superadmin()
    # Keep an explicit blank override in .env so a container-level environment value
    # cannot silently restore a key that an operator deleted from the console.
    update_env({"ASTRA_AGENT_API_KEY": ""})
    audit_record("agent_api_key.delete", "success", {"actor": actor.get("username", "admin")})
    return {"configured": False}


class AgentExchangeConfigUpdate(BaseModel):
    """Non-secret venue settings; deliberately has no API credentials or capital tiers."""
    preferred_venue: str | None = Field(default=None, pattern=r"^(okx|binance|gate|auto)$")
    routing_mode: str | None = Field(default=None, pattern=r"^(auto|balanced|split)$")
    okx_environment: str | None = Field(default=None, pattern=r"^(demo|live)$")
    binance_testnet: bool | None = None
    gate_testnet: bool | None = None
    okx_execution: bool | None = None
    binance_execution: bool | None = None
    gate_execution: bool | None = None
    venue_pools: dict[str, list[str]] | None = None
    venue_options: dict[str, dict[str, Any]] | None = None
    confirmation: str = ""

    @model_validator(mode="after")
    def validate_venue_pools(self):
        if self.venue_pools is not None:
            unknown = set(self.venue_pools) - set(VENUES)
            if unknown:
                raise ValueError(f"未知交易所: {', '.join(sorted(unknown))}")
            for venue, instruments in self.venue_pools.items():
                if len(instruments) > 500 or any(not re.fullmatch(r"[A-Z0-9]{2,15}-USDT-SWAP", str(x)) for x in instruments):
                    raise ValueError(f"{venue} 标的池仅支持合法 USDT 永续合约 ID（最多 500 个）")
        if self.venue_options is not None:
            unknown = set(self.venue_options) - set(VENUES)
            if unknown:
                raise ValueError(f"未知交易所: {', '.join(sorted(unknown))}")
            allowed = {"margin_per_trade_usdt", "max_open", "min_confidence", "dry_run"}
            for venue, options in self.venue_options.items():
                if set(options) - allowed:
                    raise ValueError(f"{venue} 路由参数只允许配置 {sorted(allowed)}")
                if "margin_per_trade_usdt" in options and not 0 <= float(options["margin_per_trade_usdt"]) <= 1_000_000:
                    raise ValueError(f"{venue}.margin_per_trade_usdt 超出范围")
                if "max_open" in options and not 1 <= int(options["max_open"]) <= 100:
                    raise ValueError(f"{venue}.max_open 必须为 1-100")
                if "min_confidence" in options and not 0 <= float(options["min_confidence"]) <= 100:
                    raise ValueError(f"{venue}.min_confidence 必须为 0-100")
                if "dry_run" in options and not isinstance(options["dry_run"], bool):
                    raise ValueError(f"{venue}.dry_run 必须为布尔值")
        return self


@agent_router.get("/api/v1/agent/capabilities")
def agent_capabilities(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    actor = require_admin_header()
    return {
        "auth_header": "X-API-Key",
        "scope": "configuration-and-telemetry",
        "granted_scopes": sorted(actor.get("scopes") or []) if actor.get("auth_method") == "api_key" else [],
        "excluded": ["initial_capital_baseline", "venue_credentials", "capital_tiers"],
        "session_only_routes": [
            "POST /api/v1/admin/risk/custom-suites",
            "PUT /api/v1/admin/risk/custom-suites/{suite_id}",
            "DELETE /api/v1/admin/risk/custom-suites/{suite_id}",
            "POST /api/v1/admin/council/profiles",
            "PUT /api/v1/admin/council/profiles/{profile_id}",
            "POST /api/v1/admin/council/profiles/{profile_id}/apply",
            "DELETE /api/v1/admin/council/profiles/{profile_id}",
            "PUT /api/v1/admin/agent-api-key/scopes",
        ],
        "scoped_routes": {
            "equity_bands:write": ["PUT /api/v1/admin/equity-bands/{domain}"],
        },
        "routes": {
            "council": ["/api/v1/admin/council/config", "/api/v1/admin/council/profiles", "/api/v1/admin/council/apply-suite", "/api/v1/admin/council/reset-role", "/api/v1/admin/council/import", "/api/v1/admin/council/export"],
            "equity_bands": ["/api/v1/admin/equity-bands/{domain}"],
            "prompt_workshop": ["/api/v1/admin/prompt-library", "/api/v1/admin/prompt-profiles", "/api/v1/admin/prompts"],
            "evolution": ["/api/v1/admin/evolution/config"],
            "risk_and_interceptors": ["/api/v1/admin/risk", "/api/v1/admin/interceptors"],
            "instrument_pool": [
                "POST /api/v1/admin/instruments",
                "DELETE /api/v1/admin/instruments/{inst_id}",
                "PUT /api/v1/admin/instruments/{inst_id}/venues",
            ],
            "models": ["/api/v1/admin/llm"],
            "exchange_routing": ["/api/v1/agent/exchanges/config"],
            "execution_telemetry": ["/api/v1/agent/telemetry", "/api/v1/admin/agents"],
        },
    }


@agent_router.get("/api/v1/agent/exchanges/config")
def get_agent_exchange_config(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> dict[str, Any]:
    require_admin_header()
    from astra_backend.exchanges import routing_policy
    from scripts.instrument_pool import load_instruments
    instruments = load_instruments()
    pools = {venue: [item.get("instId") for item in instruments
                     if venue in (item.get("venues") or ["okx"])] for venue in VENUES}
    pool_options = {venue: routing_policy.load_venue_pool(venue) for venue in VENUES}
    return {
        "preferred_venue": routing_policy.load_preferred_venue(),
        "routing_mode": routing_policy.load_routing_mode(),
        "multi_venue_routing_enabled": routing_policy.load_multi_venue_routing_enabled(),
        "okx_environment": __import__("astra_backend.config", fromlist=["settings"]).settings.okx_environment,
        "execution_enabled": {
            "okx": os.getenv("ASTRA_OKX_EXECUTION", "0") == "1",
            "binance": os.getenv("ASTRA_BINANCE_DEMO_EXECUTION" if os.getenv("ASTRA_BINANCE_TESTNET", "0") == "1" else "ASTRA_BINANCE_EXECUTION", "0") == "1",
            "gate": os.getenv("ASTRA_GATE_DEMO_EXECUTION" if os.getenv("ASTRA_GATE_TESTNET", "0") == "1" else "ASTRA_GATE_EXECUTION", "0") == "1",
        },
        "venue_pools": pools,
        "venue_options": {venue: {key: pool.get(key) for key in
                           ("margin_per_trade_usdt", "max_open", "min_confidence", "dry_run")}
                          for venue, pool in pool_options.items()},
        "instrument_pool": [{key: value for key, value in item.items()
                             if key not in {"tier", "capital_tier", "funding_tier"}}
                            for item in instruments],
        "testnet": {
            "binance": os.getenv("ASTRA_BINANCE_TESTNET", "0") == "1",
            "gate": os.getenv("ASTRA_GATE_TESTNET", "0") == "1",
        },
        "credentials": "excluded",
        "capital_tiers": "excluded",
    }


@agent_router.put("/api/v1/agent/exchanges/config")
def update_agent_exchange_config(
    payload: AgentExchangeConfigUpdate,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    actor = require_superadmin()
    from astra_backend.exchanges import routing_policy
    from astra_backend.instrument_venues import sync_venue_pools_to_master
    try:
        if payload.preferred_venue is not None:
            routing_policy.save_preferred_venue(payload.preferred_venue)
        if payload.routing_mode is not None and not routing_policy.save_routing_mode(payload.routing_mode):
            raise ValueError("非法 routing_mode")
        env: dict[str, str] = {}
        if payload.okx_environment is not None:
            env["ASTRA_OKX_ENV"] = payload.okx_environment
        if payload.binance_testnet is not None:
            env["ASTRA_BINANCE_TESTNET"] = "1" if payload.binance_testnet else "0"
        if payload.gate_testnet is not None:
            env["ASTRA_GATE_TESTNET"] = "1" if payload.gate_testnet else "0"
        execution_updates = {
            "okx": payload.okx_execution,
            "binance": payload.binance_execution,
            "gate": payload.gate_execution,
        }
        for venue, enabled in execution_updates.items():
            if enabled is None:
                continue
            phrase = f"OPEN {venue.upper()} EXECUTION"
            if payload.confirmation.strip().upper() != phrase:
                raise HTTPException(status_code=400, detail=f"变更执行开关确认短语必须精确为：{phrase}")
            if venue == "okx":
                env["ASTRA_OKX_EXECUTION"] = "1" if enabled else "0"
            else:
                testnet = getattr(payload, f"{venue}_testnet")
                if testnet is None:
                    testnet = os.getenv(f"ASTRA_{venue.upper()}_TESTNET", "0") == "1"
                axis = "DEMO_EXECUTION" if testnet else "EXECUTION"
                env[f"ASTRA_{venue.upper()}_{axis}"] = "1" if enabled else "0"
        if env:
            update_env(env)
        if payload.venue_pools is not None:
            sync_venue_pools_to_master(payload.venue_pools)
            for venue, instruments in payload.venue_pools.items():
                routing_policy.save_venue_pool(venue, instruments)
        if payload.venue_options is not None:
            if any(options.get("dry_run") is False for options in payload.venue_options.values()):
                if payload.confirmation.strip().upper() != "UPDATE VENUE ROUTING":
                    raise HTTPException(status_code=400, detail="关闭 dry_run 必须逐字确认：UPDATE VENUE ROUTING")
            for venue, options in payload.venue_options.items():
                routing_policy.update_venue_options(venue, options)
        from astra_backend.exchanges import clear_instances
        clear_instances()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("agent.exchange_routing.update", "success", {
        "actor": actor.get("username", "agent-api-key"),
        "preferred_venue": payload.preferred_venue,
        "routing_mode": payload.routing_mode,
        "execution_flags_updated": sorted(venue for venue, enabled in execution_updates.items() if enabled is not None),
        "venues_updated": sorted(set((payload.venue_pools or {}).keys()) | set((payload.venue_options or {}).keys())),
        "env_updated": sorted(env),
    })
    return {"ok": True, "config": get_agent_exchange_config()}


@agent_router.get("/api/v1/agent/telemetry")
def get_agent_telemetry(
    limit: int = 50,
    caller: str | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    require_admin_header()
    bounded = max(1, min(int(limit), 200))
    from astra_gateway.publisher import DB_PATH
    from astra_gateway.store import GatewayStore
    rows = GatewayStore(DB_PATH).model_calls(bounded)
    if caller:
        rows = [row for row in rows if row.get("caller") == caller]
    return {"items": rows, "count": len(rows), "limit": bounded, "content_stored": False}
