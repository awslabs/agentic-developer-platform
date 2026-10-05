# Tenant selection inside an ADP deployment

```bash
adp tenant list --json
adp tenant use TENANT_ID --json
adp tenant current --json
adp --deployment integration --tenant TENANT_ID models mappings list --json
ADP_TENANT=TENANT_ID adp codex
ADP_TENANT=OTHER_TENANT adp claude
```

A deployment selects the gateway. An ADP tenant selects an authorized organization inside that gateway; it is separate from a GitHub organization, AWS account or Superplane compute workspace. `tenant list` shows only visible memberships. IDs match exactly; names resolve only when exactly one visible membership has that name. Unknown and ambiguous selectors fail without choosing another membership.

Selection precedence is explicit `--tenant` before the command, a terminal's `ADP_TENANT`, a saved default for the selected gateway and login identity, then a single visible membership. Multiple eligible memberships without a selection return exit 4 (`tenant_selection_required`). Child processes inherit a canonical process pin and cannot re-read a changed default midway through the operation. `tenant current --json` includes `tenant_id`, identity, selection source and mode. `tenant use --dry-run` checks membership and reports the proposed local default without saving it.

`tenant use` changes only a private local default. It does not call `/workspaces/select`, change Cognito claims, toggle membership `is_active`, grant membership or change roles. `/workspaces/context` issues a short signed tenant lease bound to the independently verified Cognito subject, server pool, canonical workspace identity and current membership. Requests carry that lease with the original Cognito JWT in a versioned bearer envelope. The gateway verifies both and rereads membership/team scope before authorization and proxy budget enforcement. An arbitrary tenant header, locally edited JWT or another person's lease grants no authority.

The ordinary Cognito login and refresh store remains shared per deployment; refresh locking remains unchanged. The token helper obtains a fresh lease for the pinned tenant after refreshing the original token. A changed login subject or removed/replaced membership fails instead of redirecting a running session. Existing legacy workspace placement semantics remain intact; the lease does not redefine which memberships the server recognizes. Old gateways without workspace discovery retain only their existing nonempty signed single-tenant claim; explicit tenant selection requires the new exchange.

`adp codex` and `adp claude` retain their launch tenant through token refresh. Tenant/login namespaces separate proxy runtime files, logs and resumable setup state while sharing the one refresh-token store. Aliases of one deployment retain their shared underlying identity. Handoffs additionally record tenant/login metadata and refuse a resume into another tenant. Changing a saved default cannot move a running proxy or handoff. After an account switch, old running commands fail on subject mismatch and the new login receives its own default. Older defaults are retained privately under their original identity and are never reused for a different login.

Local repair commands—deployment management, help/version, login/logout, import/refresh, update and local status—remain independent of tenant selection. Task service-principal credentials continue to select their own existing Task API authority; `--tenant` is not a Task credential override. Bare tool launches outside `adp` are not promised a newly resolved tenant: launch through `adp` or preserve the established launch environment.

Exit 0 means selection/read/default save succeeded; exit 4 means selection or identity must be resolved, and normal authentication/HTTP errors preserve shared CLI classifications. Token/context secrets are not written into defaults or emitted by tenant list/current/use. Every CLI test must isolate `BG_CONFIG_DIR`, ADP stores, HOME, XDG and AWS credentials, not just change HOME.

E23 runs fresh-login tenant selection/error smoke in the existing EC2 nightly pipeline. E27 requires two existing memberships for concurrent tenant-scoped reads, local default changes and a real Cognito refresh; it preserves refreshed fixture credentials for subsequent journeys. Missing fixtures block E27. These are read/isolation regressions, not evidence that long-lived marked model inference, live revocation or uncertain mutation/resume acceptance has passed. #5622 retains that live acceptance hold until the required installed-client evidence is recorded.

To include E27 in an authorized disposable EC2 evaluation, select
`login,tenant-isolation` and supply this non-secret `fixtures_json` input:

```json
{"tenant_isolation":{"tenant_ids":["EXISTING_TENANT_A","EXISTING_TENANT_B"]}}
```

The input accepts exactly two distinct existing tenant IDs and no extra fields.
The installed fixture login must already see both memberships; the scenario
checks visibility before changing its temporary saved default. This input does
not authorize membership creation, inference or changes to the gateway address.
Evaluation and recovery receive the same validated fixture configuration.
