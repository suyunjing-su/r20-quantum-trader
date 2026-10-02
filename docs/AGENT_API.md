# Agent API

Agent integrations can manage the r20 trading configuration through the existing versioned admin APIs using a dedicated, scoped API key. This is separate from administrator passwords/sessions and legacy admin tokens. Deployments bundle two machine-readable guides: `GET /llms.txt` is the quick-start and scope overview, and `GET /llms-full.txt` is the detailed endpoint reference. Capability discovery and `/openapi.json` remain the live sources for supported paths and schemas.

## Provisioning

Set `ASTRA_AGENT_API_KEY` to a unique, high-entropy secret in the backend environment (or `.env`) and restart the backend. Do not commit a real key, put it in a URL, or send it in query parameters. Configure it in the deployment secret manager where available. Requests use:

```http
X-API-Key: <ASTRA_AGENT_API_KEY>
```

The Agent key can be generated, rotated, and deleted from **Admin → Agents** (`/admin/agents`) by a superadmin. The full value is returned only once at generation and is never readable from the status endpoint. Deleting the key revokes access immediately; the console stores an explicit disabled override so an injected environment value cannot silently restore it on restart.

## Capability discovery

`GET /api/v1/agent/capabilities` returns the supported resource groups. Existing configuration endpoints retain their HTTP methods and schemas; their administrator-session authentication also accepts this scoped key.

- Council: `/api/v1/admin/council/...`
- Equity bands: `GET /api/v1/admin/equity-bands/{domain}` is available to every Agent key for inspection; `PUT` requires the explicit `equity_bands:write` Agent scope.
- Prompt Workshop: `/api/v1/admin/prompt-library`, `/api/v1/admin/prompt-profiles...`, `/api/v1/admin/prompts`
  - Supports creating, editing, activating and deleting profiles, including enabled flags and pipeline module content/configuration.
- Evolution configuration: `/api/v1/admin/evolution/config`
- Risk: `/api/v1/admin/risk` supports reading risk configuration and applying a known built-in/custom suite or validated risk values. Custom suite lifecycle is separately protected by the optional `risk_suites:write` scope: `POST /api/v1/admin/risk/custom-suites` creates; `PUT /api/v1/admin/risk/custom-suites/{suite_id}` modifies; `DELETE /api/v1/admin/risk/custom-suites/{suite_id}` deletes. This scope is off by default and does not grant `equity_bands:write`.
- Physical interceptors: `/api/v1/admin/interceptors...`
- Instrument pool: `POST /api/v1/admin/instruments`, `DELETE /api/v1/admin/instruments/{inst_id}`, `PUT /api/v1/admin/instruments/{inst_id}/venues`. The full `GET` admin listing remains unavailable to Agent-key callers because it includes sensitive tier metadata.
- Instrument capital tiers: `GET /api/v1/agent/capital-tiers` returns a sanitized tier view; changing a symbol's tier requires `capital_tiers:write` and uses `PUT /api/v1/admin/instruments/{inst_id}/capital-tier`. The dedicated operation can only set the supported blue-chip/momentum classification and derived leverage/stop parameters. It is rejected while holdings or tracker state exist, and fails closed when holdings are unknown.
- Model connections: `/api/v1/admin/llm...`
- Venue routing: `GET|PUT /api/v1/agent/exchanges/config`
- Execution/model telemetry: `GET /api/v1/agent/telemetry`; execution-unit status is also available from `GET /api/v1/admin/agents`.

## Granting optional scopes

Agent keys start without sensitive optional scopes. A superadmin session can explicitly grant or revoke scopes without rotating the key. The request replaces the complete grant list, so include every scope that should remain enabled:

```http
PUT /api/v1/admin/agent-api-key/scopes
X-Astra-Session: <superadmin-session>
Content-Type: application/json

{"scopes":["equity_bands:write","risk_suites:write","capital_tiers:write"]}
```

Available optional scopes:

- `equity_bands:write`: permits `PUT /api/v1/admin/equity-bands/{domain}` for council, prompt, and risk mappings. GET remains available without this scope.
- `risk_suites:write`: permits custom risk suite create/update/delete at the three `/api/v1/admin/risk/custom-suites` routes listed above. It does not grant initial capital, exchange credential, or equity-band write access.
- `capital_tiers:write`: permits `PUT /api/v1/admin/instruments/{inst_id}/capital-tier` to change the instrument tier between the supported classifications. It does not grant access to the full instrument listing or changes to base size, risk budget, venues, or credentials.

Read-only tier metadata can be fetched from `GET /api/v1/agent/capital-tiers` without this write scope; the response is deliberately limited to instrument ID/name, tier label/ID, maximum leverage, and stop-loss ATR multiplier. Tier writes are rejected while positions/tracker entries exist, and return 503 if current holdings state is unknown.

The grant list is persisted as `ASTRA_AGENT_API_KEY_SCOPES`. `GET /api/v1/agent/capabilities` reports granted scopes and the matching `scoped_routes`. The scope-management endpoint itself is never available to Agent API keys, preventing a machine key from self-escalating. The Agents panel exposes independent switches for these grants.


```bash
curl -X POST http://localhost:8080/api/v1/admin/risk \
  -H 'X-API-Key: <key>' -H 'Content-Type: application/json' \
  -d '{"values":{"ASTRA_MAX_LEVERAGE":5}}'
```

The venue-routing endpoint only accepts non-secret environment/routing values, per-venue instrument lists, and bounded routing options (`margin_per_trade_usdt`, `max_open`, `min_confidence`, `dry_run`). It does not accept exchange credentials or capital-tier fields. Disabling `dry_run` and changing an execution flag require the existing exact confirmation phrases. Venue pools synchronize with the master instrument pool.

## Deliberate exclusions

Agent keys cannot access `/api/v1/admin/account-baseline`, manual-close/account execution endpoints, exchange credential updates/tests, account snapshots or other admin resources outside the explicit allowlist. Initial capital baselines, the three exchanges' access credentials, and scope administration remain administrator-session-only. Equity-band writes require `equity_bands:write`; custom risk suite create/update/delete requires `risk_suites:write`; instrument-tier writes require `capital_tiers:write`. The tier-list endpoint is sanitized and does not expose full instrument-pool capital metadata. All grants are independent and disabled by default.

All writes performed through the API remain subject to the existing validation, confirmation phrases, and audit logging. Telemetry contains model call metadata only; prompts and responses are not persisted or returned.
