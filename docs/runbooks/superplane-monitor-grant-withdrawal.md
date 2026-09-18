# Superplane platform-monitor grant withdrawal

This procedure moves the Superplane platform monitor off its direct Aurora table
grant and onto the API's authenticated observation contract, then withdraws the
grant — in that order, with a continuity demonstration between each step.

Issue #5056 (U15) wrote the code. **This runbook does not authorize running
itself.** Deployment of the receiver and the monitor image belongs to U23
(#5327); the `REVOKE` is a change to a live database's authorization state and
needs a named authorizing operator. Follow the
[deployment guide](../adp-platform-deployment/deploy-with-agent.md) for account
and environment confirmation.

## Substitutions used below

| Placeholder | Value | Where to get it |
|---|---|---|
| `$API` | Base URL of the Superplane API | `http://superplane-api.superplane-system.svc.cluster.local:8000` in-cluster |
| `<schema>` | The API's own Postgres schema | `database_schema` in `infra/control-plane`; never `public` |
| `<monitor_role>` | Database role the monitor authenticates as | The role in the running monitor's `DATABASE_URL` secret — read it, do not guess, do not paste it into a shared channel |

## Why the order is load-bearing

R11 acceptance 1 is *"with the monitor's direct table grant withdrawn, monitoring
still functions"*. That wording asks for proof the direct-write path is **gone**,
not merely unused — which is why the withdrawal is a step here rather than an
afterthought. Code that no longer issues the SQL is necessary but not sufficient:
a process still holding the privilege is one configuration change away from using
it again, and nothing observes the difference until an incident.

The sequence is therefore fixed, and each step is safe to stop at:

| # | Step | Reversible by |
|---|---|---|
| 1 | Apply migration `008` (two new tables, additive) | `alembic downgrade` — no domain data involved |
| 2 | Deploy the receiver (`legacy_heartbeat_enabled` still **true**) | Redeploy previous image |
| 3 | Configure the submitter credential | Delete the secret |
| 4 | Deploy the monitor that sends over the contract | Redeploy previous image (still has `DATABASE_URL`) |
| 5 | **Demonstrate continuity** | — (observation only) |
| 6 | Set `legacy_heartbeat_enabled=false` | Set it back to true |
| 7 | **`REVOKE` the grant** | Re-grant — see [Rollback](#rollback), which is not a code revert |

Steps 1–4 are all additive: after step 4 both write paths exist and either can
carry traffic. That overlap is the whole point — it is what makes step 5 a
measurement rather than a hope. **Do not compress steps 6 and 7 into step 4.**

## What the grant actually was

Before #5056 the monitor connected to Aurora directly and needed, on the API's
own schema:

| Table | Privilege | Used by |
|---|---|---|
| `clusters` | `SELECT` | `ListActiveClusters` — 12 columns, all active clusters, **unscoped by workspace** |
| `clusters` | `UPDATE` | `UpdateClusterHealth` — `health_status`, `last_reconciled_at`, `actual_state_json` (the receiver writes the same three columns, and deliberately not `last_heartbeat` — see step 5 check 1) |
| `events` | `INSERT` | `InsertEvent` — health-transition rows, `org_id` supplied by the caller |
| `events` | `SELECT` | `GetCostHistory` — `details_json->>'cost_hourly'` over a time window |
| `reconcile_locks` | `INSERT`, `UPDATE`, `DELETE` | `AcquireLock` / `ReleaseLock` |

Note the second column: **the observation write was never the whole grant.** A
change that replaced only `UpdateClusterHealth` would leave four of those five
rows required, and the `REVOKE` would fail closed on the monitor's next poll. That
is why #5056 added scoped API routes for the cluster list, the cost history and
the event write as well, and why the verification below exercises all of them
rather than just the heartbeat.

Note also what the `SELECT` on `clusters` conferred: every cluster in the
database, for every tenant. The replacement route derives its filter from the
credential's grant and takes no parameter that can widen it.

## Preconditions

- [ ] **Migration `008_add_observation_receiver_tables` is applied.** It creates
      `observation_receipts` and `observation_leases`. Additive only — no column
      added to or removed from an existing table, no data migration.
- [ ] **Blocker, read this before scheduling:** the Alembic chain does not have a
      single head (three files declare `006`, three declare `007`), so
      `alembic upgrade head` cannot resolve it and `check_migration_contract.py`
      refuses to run. `releases/superplane.lock.yaml` records
      `status: unverified`, `single_head: false`. **U13 (#5045) owns that repair,
      and this procedure cannot start until it lands.** `008` is written to be
      re-parented by U13 rather than to pre-empt it.
- [ ] Receiver image built from a revision containing `POST /internal/observations`.
- [ ] Monitor image built from a revision whose `config.Config` has no database
      field (`OBSERVATION_API_URL` present, `DATABASE_URL` absent).
- [ ] A named operator authorized to change database grants, and a named rollback
      owner. Do not begin without both.
- [ ] The three substitutions above resolved, in particular `<monitor_role>`: the
      `REVOKE` must name the grantee the monitor actually authenticates as.

## Step 1–2: receiver first

Deploy the API with the receiver present and `legacy_heartbeat_enabled` left at
its default `true`. Both write paths now exist. Nothing has been asked to change
behaviour, so a failure here is a receiver problem in isolation — which is the
reason to take this step alone.

Confirm the route exists and refuses an anonymous caller:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST "$API/internal/observations" \
  -H 'content-type: application/json' \
  -H 'x-superplane-contract-version: v1' \
  --data '{"contract_version":"v1"}'
```

Expect `401`. Read what this does and does not show: it presents no credential at
all, so it proves the route is deployed and rejects an unidentified submission —
it does **not** test whether `observation_submitters` is configured, because
nothing was offered for the receiver to resolve. The accepted-submission code is
`202`, so any `2xx` here would mean an unauthenticated write succeeded; stop
immediately if you see one.

The receiver is fail-closed by construction — `observation_submitters` is empty by
default, and with no entries no credential resolves — but that property is
asserted by the test suite, not by this curl.

## Step 3: the submitter credential

The monitor authenticates with a credential and signs with an HMAC key. Both live
in the deployment's secret store as `superplane-observation-submitter`
(`credential`, `signing_key`), and the receiver resolves them from
`observation_submitters` — a JSON array whose entries carry `submitter_id`,
`credential`, `signing_key` and the `workspaces` that submitter may write.

**The `workspaces` list is the tenant boundary.** It is the only thing standing
between an authenticated monitor and a cross-workspace write, and it comes from
configuration, not from the payload. Grant the monitor exactly the workspaces it
must observe. An empty grant authorizes nothing (deliberately fail-closed); a
wildcard is not supported and must not be simulated by enumerating every
workspace.

Never log, echo or paste either value. They are as sensitive as the database
password they replace: a party holding the signing key can mint an observation
for any cluster inside that submitter's grant.

## Step 4: the sender

Deploy the monitor image whose `deploy/deployment.yaml` injects
`OBSERVATION_API_URL`, `OBSERVATION_CREDENTIAL` and `OBSERVATION_SIGNING_KEY`,
and **no `DATABASE_URL`**. `config.LoadFromEnv` requires all three with no
defaults and no fallback to `DATABASE_URL` — that fallback is exactly what would
keep the direct-write path alive past the withdrawal.

Confirm the pod holds no database credential:

```bash
kubectl -n superplane-system get deploy superplane-platform-monitor \
  -o jsonpath='{.spec.template.spec.containers[0].env[*].name}{"\n"}'
```

`DATABASE_URL` must not appear. `/healthz` on the metrics port returning `200`
means the scoped list route answered, which also proves authentication works;
`503` reports only "observation API unavailable" and deliberately does not
distinguish "credential rejected" from "API down", so read the pod logs for the
reason rather than the probe body.

## Step 5: demonstrate continuity — before withdrawing anything

This is the step that turns the withdrawal from a hope into a decision. All four
must hold. Run them against the deployed pair, with the grant still in place, so
that a failure is recoverable by doing nothing.

1. **Observations advance — and the controller's heartbeat is not touched.** Pick
   a monitored cluster and confirm its `reported_at` moves across at least two
   poll intervals (default 30s), read through
   `GET /internal/observations/{cluster_id}` — not by querying the table, which
   would test the path being retired. `last_reconciled_at` in
   `GET /internal/observations/clusters` advances with it.

   **Do not check that `last_heartbeat` moves.** It must *not*: that column means
   "the data-plane controller last reported in", and it is the input to the
   monitor's own `heartbeat_freshness` check and to `_effective_health_status` in
   the API. A receiver that stamped it from the monitor's submission would make
   this step unfalsifiable — the timestamp would refresh on every poll whether or
   not the controller was still alive, and the "heartbeat missing" escalation
   could never fire again. So a `last_heartbeat` that stops moving after step 6
   is a **real finding about that cluster's controller**, not a receiver
   regression; see step 6's caller inventory.
2. **Health is recorded and honest.** The recorded status reflects real checks:
   dimensions nothing inspected read `not_checked`, not `healthy`. See
   [Expect health to look worse](#expect-health-to-look-worse-and-why-that-is-the-fix).
3. **Locking still serializes.** With two replicas running, exactly one holds
   each lease scope. `observation_leases.acquire_count` advances and
   `fence_token` is monotonic; it must never restart at a lower value.
4. **Foreign writes fail.** Submit an observation for a cluster outside the
   monitor's `workspaces` grant and confirm refusal. Authentication is not
   sufficient — the submitter must own the subject — so this is the check that
   the scoping, and not merely the signature, is live.

   Expect **`403`** for a foreign *write* and **`404`** for a foreign *read*. The
   asymmetry is intentional and not a bug to report: a write refusal admits the
   caller authenticated and lacked authority, while a read of a cluster outside
   the grant is answered identically to a read of a cluster that does not exist,
   so the route cannot be used to discover which cluster UUIDs are real. If you
   see `404` on a foreign read, that is the control working — do not go looking
   for a deleted cluster.

`src/superplane-platform-monitor/tests/integration_test.go` automates 1–4 against
a deployed receiver (`go test -tags=integration ./tests/...` with the
`INTEGRATION_OBSERVATION_*` variables set). It skips without them, and the CI lane
does not pass `-tags=integration`, so it never runs there.

## Step 6: retire the unauthenticated route

Set `legacy_heartbeat_enabled=false` and redeploy the API.
`POST /internal/heartbeat` then answers `410 Gone`.

This closes the hole the story is about: the legacy route needs the caller to
know a cluster UUID and nothing else — no identity, no signature — so a forged
cluster-state write by UUID alone succeeds while it is enabled. **Until this step
lands, acceptance 2 is not met no matter how well the contract works.**

Check for other producers before flipping it. The Superplane controller on each
data plane also posts heartbeats; if any still uses the legacy route, this step
silently stops its reporting, and the monitor will then correctly report those
clusters as unreachable. Inventory the callers, then flip.

## Step 7: withdraw the grant

With the authorizing operator present, revoke exactly the privileges tabulated
above from the monitor's role — not more:

```sql
-- Substitute the real role name; <schema> is the API's own schema, never `public`.
REVOKE SELECT, UPDATE ON <schema>.clusters         FROM <monitor_role>;
REVOKE SELECT, INSERT ON <schema>.events           FROM <monitor_role>;
REVOKE INSERT, UPDATE, DELETE ON <schema>.reconcile_locks FROM <monitor_role>;
```

Then re-run all four checks from step 5. They must all still pass. **That is
acceptance 1**: monitoring functioning while the privilege is absent is the proof
the direct path is gone rather than dormant.

If the role exists only to serve the monitor, dropping it is cleaner than
revoking table by table — a role retaining `CONNECT` is a smaller finding than a
role retaining `UPDATE`, but it is still a credential nothing needs. Confirm no
other workload authenticates as it first.

Narrow the IRSA role in the same change window
(`serviceAccountName: superplane-platform-monitor`): the monitor no longer needs
Aurora data-API or IAM-auth access for its own reads and writes. In-database
grants and IAM permissions are two independent gates, and leaving the second one
open preserves a path to the first.

## Expect health to look worse, and why that is the fix

After the monitor deploy, recorded health will look *worse* than before for some
clusters. This is the intended result of R11 acceptance 4 and not a regression:

- `NoopEKSProber` used to return `nil` — success — without contacting anything.
  `UnconfiguredEKSProber` now reports `not_checked`, because no real
  cross-account probe exists yet. The `eks_reachability` dimension stops claiming
  a reachable Kubernetes API that nothing ever contacted.
- `checkVaultSyncStatus` reached `Healthy` on **every** branch, so it could only
  ever say "fine".
- The severity ordering ranked `Unknown` above `Unreachable`, so a cluster that
  was demonstrably unreachable could be reported as merely unknown.

**One live defect this exposes rather than fixes.** The controller emits
`vault_sync_status: "synced"`, which the API's `^(ok|failed|pending)$` schema does
not allow. Under the old code that unrecognised value fell into a `default` branch
reporting `Healthy`, so the mismatch was invisible; it now reports `Unknown`,
which is what "I was told something I cannot interpret" means. So expect the
`vault_sync_status` dimension to go `Unknown` fleet-wide at cutover. **That is a
pre-existing producer/schema disagreement becoming visible, not a new fault, and
aligning the two is not #5056's change.** File it against the controller. Do not
"fix" it by mapping `synced` to healthy in the monitor — that restores the
dishonest reading and re-hides the mismatch.

Note that `vault_sync_status` is both a controller-reported key in
`actual_state_json` and a monitor dimension name. The receiver therefore records
check results **inside** the `observation` sub-document rather than at the top
level, so the monitor's verdict for a dimension can never overwrite the
controller-reported value that dimension is judging. If you are reading recorded
state during this procedure, read check results from
`actual_state_json.observation.checks_performed` and
`checks_not_performed`; the top-level keys are the producer's and are left alone.

Brief a human before cutover: a dashboard turning yellow is the change working.
Tell whoever watches it, or someone will roll this back at 2am.

## Rollback

**Rollback is not a code revert, and the code revert is not the interesting part.**

Rolling back a deploy (steps 2–4, 6) is ordinary: redeploy the previous image, or
set `legacy_heartbeat_enabled` back to `true`. Reverting step 7 is not, and needs
naming explicitly:

- **A re-grant is an authorized, recorded expansion of authority**, performed by
  the authorizing operator — not a routine `kubectl` action and not something an
  agent should do. It restores a privilege that a tenant-boundary control now
  depends on being absent.
- **Audit the temporary expansion afterwards.** Record who re-granted what, when,
  why, and when it was withdrawn again. A re-grant that nobody wrote down is
  indistinguishable from the grant never having been withdrawn.
- **Observations already written through the API are retained.** They are real
  recorded state in `observation_receipts` and the domain tables, not a staging
  buffer, so no rollback step deletes them. Do not "clean up" after a rollback:
  those rows are the only record of what health actually was during the window.
- **`observation_leases` rows are retained on release** so `fence_token` never
  restarts. Truncating that table to "reset" locking defeats fencing, exactly as
  the old `reconcile_locks` delete-on-release did. Never truncate it as a recovery
  action.
- **Rolling back the monitor image reintroduces `DATABASE_URL`.** So a monitor
  rollback *requires* the grant back — the two are coupled and must be rolled back
  together, in the reverse of the order above (re-grant, then redeploy). Rolling
  back the image while the grant is withdrawn produces a monitor that cannot start
  and reports nothing, which is worse than either end state.

## What this procedure does not establish

- It does not verify that no *other* workload writes those tables directly. It
  withdraws one role's grant, and the code change proves the monitor no longer
  needs it. Other producers are out of scope and were not inventoried.
- It confers no budget authority. The monitor reports observed spend and does not
  enforce a limit; admission-time enforcement is M6's and is unaffected.
- It does not align the controller's `vault_sync_status` values with the schema.
- It makes no claim about backup, retention or restore of the two new tables.
