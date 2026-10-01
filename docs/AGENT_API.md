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
- Risk: `/api/v1/admin/risk` (including reset and applying a saved custom suite by `custom_suite_id`). Custom suite create/update/delete routes are intentionally unavailable to Agent API keys; use a superadmin session.
- Physical interceptors: `/api/v1/admin/interceptors...`
- Instrument pool: `POST /api/v1/admin/instruments`, `DELETE /api/v1/admin/instruments/{inst_id}`, `PUT /api/v1/admin/instruments/{inst_id}/venues`. The `GET` admin listing is intentionally unavailable to API-key callers because it includes capital-tier metadata.
- Model connections: `/api/v1/admin/llm...`
- Venue routing: `GET|PUT /api/v1/agent/exchanges/config`
- Execution/model telemetry: `GET /api/v1/agent/telemetry`; execution-unit status is also available from `GET /api/v1/admin/agents`.

## Granting optional scopes

Agent keys start without sensitive optional scopes. A superadmin session can explicitly grant or revoke the equity-band write scope without rotating the secret:

```http
PUT /api/v1/admin/agent-api-key/scopes
X-Astra-Session: <superadmin-session>
Content-Type: application/json

{"scopes":["equity_bands:write"]}
```

The scope grant is persisted as `ASTRA_AGENT_API_KEY_SCOPES`. `GET /api/v1/agent/capabilities` reports the scopes granted to the calling Agent key. The scopes-management endpoint itself is never available to Agent API keys, preventing a machine key from self-escalating.


```bash
curl -X POST http://localhost:8080/api/v1/admin/risk \
  -H 'X-API-Key: <key>' -H 'Content-Type: application/json' \
  -d '{"values":{"ASTRA_MAX_LEVERAGE":5}}'
```

The venue-routing endpoint only accepts non-secret environment/routing values, per-venue instrument lists, and bounded routing options (`margin_per_trade_usdt`, `max_open`, `min_confidence`, `dry_run`). It does not accept exchange credentials or capital-tier fields. Disabling `dry_run` and changing an execution flag require the existing exact confirmation phrases. Venue pools synchronize with the master instrument pool.

## Deliberate exclusions

Agent keys cannot access `/api/v1/admin/account-baseline`, manual-close/account execution endpoints, exchange credential updates/tests, account snapshots or other admin resources outside the explicit allowlist. In particular, initial capital baselines, the three exchanges' access credentials, and scope administration remain administrator-session-only. Equity-band writes are separately protected by the `equity_bands:write` grant; without it, the key can read bands but receives 403 on writes.

All writes performed through the API remain subject to the existing validation, confirmation phrases, and audit logging. Telemetry contains model call metadata only; prompts and responses are not persisted or returned.
