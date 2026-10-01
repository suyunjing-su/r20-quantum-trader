# Council Profiles API

The council keeps the existing active configuration in `data/council_config.json` and stores user-managed saved configurations separately in `data/council_profiles.json`.

Supported operations:

- `GET /api/v1/admin/council/profiles`
- `POST /api/v1/admin/council/profiles`
- `PUT /api/v1/admin/council/profiles/{profile_id}`
- `POST /api/v1/admin/council/profiles/{profile_id}/apply`
- `DELETE /api/v1/admin/council/profiles/{profile_id}`

Write and apply operations require a superadmin session. Each profile contains the complete council configuration, including enabled state, consensus mode, timeout, and role/model/prompt parameters. Existing council role and model-binding validation remains active for both save and apply operations.

Council profiles may be independently selected by settled-equity bands using `/api/v1/admin/equity-bands/council`; this does not alter prompt-library or risk-suite band mappings.
