# Platform persona model defaults

Platform administrators can set or reset a model for each registered, configurable
persona in Settings → Agent Models → Platform persona defaults. Ordinary users
continue to use the existing persona model selector and **Use default** action.
Service-account preference management keeps its existing authorization rules.

Resolution for new runs is: explicit invocation model, saved principal preference,
platform persona default, then the SDK compatibility-class default. Invalid
explicit or saved choices retain the existing refusal behavior. Model compatibility,
tenant restrictions and live runtime posture remain enforced. Existing mapping
and runtime feature flags are unchanged.

The administrator API is `GET /admin/persona-defaults` and
`PUT /admin/persona-defaults/{persona_key}`. Writes require a canonical model ID
(or null to reset), expected revision (zero for creation), and change reason.
An optional operation UUID makes successful retries idempotent. Atomic revision
checks prevent lost updates; changes and rejected writes are audited. Promotion
reuses the existing exact SDK evidence and platform destination checks.

Migration 081 creates an initially empty global table. No unproven model is
activated by migration. The admin selector suggests `openai.gpt-6-astra` for
registered Codex architect, reviewer and AI-DLC personas, and `openai.gpt-6-sol`
for developer and operations. Suggestions apply only where the authoritative
registry assigns the persona to the Codex SDK. This work does not register or
enable additional personas.

Root policy snapshots capture persona defaults and revisions. New snapshots with
persona-default rows use schema version 2; readers supporting only version 1
reject them instead of silently ignoring the defaults. Existing version 1 snapshots
retain their format and digest. Reset rows retain their revision to prevent stale
writes. Active runs and their children keep their recorded defaults.

Apply the migration and deploy compatible gateway readers before setting defaults.
No deployment or live default activation is included in this change.

Validation covers administrator authorization, promotion evidence, incompatible
models, stale revisions, idempotent replay, reset, dispatch precedence, database
snapshot freezing, backwards compatibility, and the existing user settings UI.
