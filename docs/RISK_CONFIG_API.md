# Risk configuration API

风控参数唯一 schema 与校验规则由 `astra_backend.risk_config` 提供；预设套件（`conservative`、`balanced`、`aggressive`）和自定义方案是两个独立集合。所有方案值使用原生单位（比例使用小数），自定义方案必须包含 schema 中的完整参数键集合。

## Read current configuration

`GET /api/v1/admin/risk[?equity=N]` requires an administrator session. The response includes `schema`, built-in `suites`, saved `custom_suites`, current values and process/effective-engine snapshots. Each custom suite has:

```json
{
  "id": "<server-generated-id>",
  "name": "Low-volatility account",
  "description": "Optional notes",
  "values": { "ASTRA_MAX_LEVERAGE": 5 }
}
```

The abbreviated `values` object above is illustrative; create/update requests must contain **every** risk parameter in the current schema.

## Create, update, delete custom suites

These management operations require a superadmin session (`X-Astra-Session`). They are intentionally not in the Agent API-key scope.

- `POST /api/v1/admin/risk/custom-suites` creates a suite and returns `{ "suite": ..., "custom_suites": [...] }`.
- `PUT /api/v1/admin/risk/custom-suites/{suite_id}` replaces the suite's name, description, and complete values.
- `DELETE /api/v1/admin/risk/custom-suites/{suite_id}` removes the saved suite. Deleting a suite does not change the active risk configuration.

Request body for POST/PUT:

```json
{
  "name": "Low-volatility account",
  "description": "Optional notes (up to 240 characters)",
  "values": {
    "ASTRA_MAX_CONCURRENT_POSITIONS": 4,
    "ASTRA_MAX_SAME_DIRECTION_POSITIONS": 2
  }
}
```

The values snippet is abbreviated for readability; submit the full set from `GET /api/v1/admin/risk` (or its selected suite). Values are normalized and checked against schema bounds and cross-field constraints before persistence. Invalid, missing, or extra keys are rejected. Data is stored separately from `.env` in an atomically replaced JSON file under `data/`; saving a profile does not apply it.

## Apply a saved custom suite

POST `/api/v1/admin/risk` with `custom_suite_id` to apply a stored profile through the regular risk configuration path:

```json
{
  "custom_suite_id": "<server-generated-id>",
  "confirmation": ""
}
```

Application writes the normalized values to `.env`, refreshes backend risk constants, synchronizes leverage caps, and records an audit event. If any parameter crosses a configured high-risk threshold, repeat the request with the exact confirmation phrase `HIGH RISK`; otherwise the API responds with HTTP 400 and the affected fields. Built-in `suite_id` and `custom_suite_id` cannot be combined, and custom-suite application cannot be combined with an explicit `values` object.

An Agent API key may read saved suites through the already-allowlisted risk GET endpoint and may apply a known custom suite ID via the existing risk update endpoint. It cannot create, modify, or delete custom suites. All apply requests retain the same validation, high-risk confirmation, and audit behavior as administrator requests.

## Built-in suite and reset compatibility

Existing requests remain supported:

- `POST /api/v1/admin/risk` with `{ "suite_id": "balanced" }` applies a built-in suite.
- `POST /api/v1/admin/risk` with `{ "values": { ... }, "confirmation": "" }` updates selected parameters.
- `POST /api/v1/admin/risk/reset` with `{ "confirmation": "RESET RISK" }` clears risk overrides and restores code defaults. Reset does not delete saved custom suites.

See `/openapi.json` and `GET /api/v1/agent/capabilities` for the live API contract and Agent-key scope.
