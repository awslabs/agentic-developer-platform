# A03 source configuration reconciliation

Owner: #5655, contributing to #5677/#5599. This is the reviewed **source-only**
package, not a claim that any serving environment has been deployed or accepted.
The three exact finding records were read from the private 2026-09-21 run ledger;
raw scanner content and credentials are not included in this repository.

## Per-finding disposition

| Finding | Source disposition and contributing changes |
|---|---|
| `f-0054d7ee-bbe7-4b3d-8f88-91695dbc247f` | A02 commit `3cb303b00` / PR #5786 requires verified research tenant/actor and preserves tenant filtering in legacy mode. A01 commit `6455f8b5c` / PR #5737 validates gateway caller/client binding. The budget target-read half is A12 #5668 / PR #6087, merged at `b76e9d4880a87fae787a24af64dd01faed91b3e1`. Its source acceptance and CI were recorded before A03 closure. A03 changes no budget authorization implementation. |
| `f-16d7f58c-3049-4afc-8715-99b824be7108` | The remaining default mismatch is repaired here: Superplane strict authentication and Door tenant scoping default on; disabled auth/isolation require explicit development profiles. CORS was repaired by A02; Cognito access-token/client binding by A01. Door readiness/provenance filtering is A06 commit `e6e8f3226` / PR #5790; the integrated S15 public-provenance marker checks are `b361419c0` / PR #6079. Shared rate-limit persistence/counters are A14 PR #6085, merged at `f12603324954d792d628eb0a1ab6dce991bf35ff`; A03 additionally requires its memory opt-in to use the development profile. |
| `f-93db06c3-561a-49cf-8f42-16c66137301c` | A04 commit `a5f3570cf` / PR #5739 removed embedded signing/database secrets and requires secret references. A02 repaired research authentication, actor attribution and CORS; A01 validates the proxy caller. This change aligns the legacy API manifest/template with the strict installer and code default. Missing issuer/client allowlist cannot construct a production policy; missing signing key is already a startup refusal. |

The constituent auth, tenant ownership, token validation, budget and rate-limit
implementations are reused. There is no second permission or identity resolver.
A12 and A14 source dependencies merged with checks passing. A03 merged in PR
#6088 at `3d74b256484d11973ade7d92ecf5aa9d259c9925`, and #5655 closed with
source-acceptance evidence. The table records contributing source revisions, not scanner status
updates or evidence of a live exploit attempt.

## Defaults and installer agreement

- Superplane defaults to `SUPERPLANE_SECURITY_PROFILE=production` and
  `DOMAIN_AUTH_ENFORCED=true`. The installer and legacy Deployment pin both.
  The legacy manifest requires `COGNITO_ISSUER`, `COGNITO_JWKS_URL` and
  `DOMAIN_AUTH_ALLOWED_CLIENT_IDS` ConfigMap keys. Empty values fail policy
  construction; operators must supply the reviewed issuer/client set first.
- CORS remains empty by default and rejects `*`. The installer uses its validated
  origin; the legacy template uses valid JSON `[]` for same-origin service.
  Signing secrets remain empty in the template and required by secret reference.
- Door defaults to authenticated, tenant-scoped reads. Its ACL-store constructor
  also defaults to the scoped query. Missing/unavailable ACL storage still denies
  under A06. Both auth and tenant-scoping opt-outs require
  `DOOR_SECURITY_PROFILE=development`; the shipped ConfigMap pins production.
  Unknown tenant-switch values cannot silently disable the boundary. Project
  filtering remains an optional organizational view, not a tenant-boundary switch.
- Gateway selects shared Redis under A14. Memory requires both
  `RATELIMIT_ALLOW_MEMORY_BACKEND=true` and `RATELIMIT_SECURITY_PROFILE=development`
  with the memory backend, or the existing explicit `TESTING=1` harness. The
  shipped gateway ConfigMap pins production and Redis. Shared Settings has no
  general environment field, so this is a dedicated, validated security profile.

Development exceptions require explicit configuration. They are not a way to
roll back a production authentication or shared-state outage. Preserve the
production profile, supply missing dependencies and use the normal rollout.

## Verification scope

`tests/internal/test_security_defaults_reconciliation.py` loads actual component
settings without security environment overrides, tests forbidden opt-outs and
unknown values, and compares shipped manifest settings and required references.
The installer test renders its actual manifest with a reviewed synthetic origin
and client allowlist. Existing Door auth/ACL, Superplane auth/research, gateway
JWT/proxy and A14 rate-limit suites retain responsibility for runtime behavior.

No IAM changes, deployment, secret rotation, historical data cleanup or paid scan
is part of this package. Existing signed sessions, deployed profiles, Redis
connectivity and current tenant access must be assessed by their separate rollout
owners. Merged source alone is not live acceptance.
