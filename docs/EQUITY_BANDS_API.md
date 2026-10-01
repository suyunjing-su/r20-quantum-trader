# Equity Band and Strategy Profile API

## Independent equity bands

The three strategy domains are independent and are not combined into one profile:

- `GET /api/v1/admin/equity-bands/council`
- `GET /api/v1/admin/equity-bands/prompt`
- `GET /api/v1/admin/equity-bands/risk`
- `PUT /api/v1/admin/equity-bands/{domain}` (superadmin session)

The request body is:

```json
{
  "bands": [
    {"id":"small","min_equity":0,"max_equity":500,"target_id":"conservative"},
    {"id":"large","min_equity":500,"max_equity":null,"target_id":"balanced"}
  ]
}
```

Intervals are half-open: `[min_equity, max_equity)`. A null maximum is unbounded. Overlapping enabled intervals are rejected; disabled intervals are retained but are not resolved. The maximum is 100 bands per domain. Unknown equity never resolves a band.

`target_id` is interpreted in the domain that owns the band:

- council: a saved council profile ID;
- prompt: an existing prompt profile ID;
- risk: a built-in or custom risk suite ID.

## Council profiles

- `GET /api/v1/admin/council/profiles`
- `POST /api/v1/admin/council/profiles` (superadmin session)
- `PUT /api/v1/admin/council/profiles/{profile_id}` (superadmin session)
- `POST /api/v1/admin/council/profiles/{profile_id}/apply` (superadmin session)
- `DELETE /api/v1/admin/council/profiles/{profile_id}` (superadmin session)

A profile stores the complete council configuration (`enabled`, `consensus_mode`, `timeout_seconds`, and `roles`). Role structure and model bindings are validated when a profile is saved and again when it is applied. Invalid or unavailable model bindings are rejected rather than silently converted.

## Runtime behavior

When a caller supplies calculation equity, prompt and council resolution can select the matching independent profile. Risk reads expose `equity_band` and `resolved_values` for the matching risk suite. The authoritative production calculation-equity source is the single open venue's settled equity when multi-venue routing is disabled; unavailable values remain unknown rather than becoming zero.
