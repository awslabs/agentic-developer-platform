# Wave 2 operator fixture — issue #3968

Executable fixture tooling for the Wave 2 live evaluation.

## Status: live validation in progress

The maintainer has executed isolated real-SDK fixture runs in account
`879318057152`. These runs exposed collector and timeout-probe defects now being
repaired. **Wave 2 is not accepted.** Source checks, image verification, individual
experiment success and full live acceptance are separate milestones. Current
run status belongs to [evaluation #3968](https://github.com/aws-e/adp/issues/3968).

Operator tests use controlled AWS/Kubernetes transports; they cannot establish
live isolation or SDK behavior. Root owns live mutations, evidence collection,
cleanup and PR review/merge. Credentials stay outside this directory.

## Target, as previously observed (re-verify before use)

These are readings from the earlier operations run, kept because they document
what `00-verify-target.sh` checks and why. They are **not** a current-state
assertion — the script re-reads every one of them at execution time and refuses
if any has drifted.

| Fact | Previously observed |
|---|---|
| Account / role | `879318057152` / `ADP-Agent-adp-embark1` |
| Ambient shell (**wrong, do not use**) | `605440105851` / `ADP-Agent-adp-embark2` |
| Invocation table | `adp-dev-webhook-events`, `event_id` HASH + `arrived_at` RANGE |
| Table scale | ~391,659 items — **shared**, so W2-07 must use deltas |
| `FEATURE_AGENT_CONTROL_ENABLED` (ordinary gateway) | `false` — DP-INV-1 intact |
| NetworkPolicy objects in `adp-agents` | 9, controller enforcing |
| Claude SDK pin | `0.3.220` in `package.json` and `package-lock.json` |
| Ordinary gateway digests | settled on one (`sha256:da18a4d6…`, all 10 pods) |

The ambient-vs-connection divergence is the #5195 failure mode: a bare
`aws sts get-caller-identity` is *not* evidence you are on the fixture account.

### Credential modes

`lib/session.sh` accepts three, and asserts the **account** in all of them. The
account is the security property; the credential mode is not, and the role name
is recorded rather than gated (several legitimate credentials reach this
account, and refusing on the role name rejects a correct one).

| Mode | Selected by | Notes |
|---|---|---|
| vault | `adp-cred` on `PATH` | Preferred where available |
| profile | `AWS_PROFILE` | Root's path: valid credentials, no `adp-cred` binary |
| ambient | `AWS_ACCESS_KEY_ID` / instance role | Accepted, but this is the #5195 shape — the account assertion is what catches it |

Whichever mode is in play, a credential resolving to any other account refuses
before a single observation is read, let alone anything created. Earlier, step 00
hard-required `adp-cred` and refused without it — a guard that cannot execute in
its intended environment protects nothing, so the single account assertion now
covers all three modes and is strictly stronger than the binary check it
replaced.

## Scripts

Run in numeric order. Each prints `ok`/`FAIL` lines and writes its artifacts into
`<evidence-dir>/artifacts/`, which is what the harness config points at.

| File | What it does | Needs |
|---|---|---|
| `run-all.sh` | **Orchestrator.** Sequences every step below, threads one run id, gates on prerequisites, and guarantees cleanup via an `EXIT` trap (also on failure and Ctrl-C). Default is validate-only — creates nothing without `--apply`. A skipped step is counted as skipped, never as a pass, and any skip makes the run exit non-zero. `--stage gateway` / `--stage worker` drive the two-invocation staged path a protected worker requires (see the Runbook) | AWS credential (any mode) |
| `00-verify-target.sh` | Asserts the target **account** (via `lib/session.sh`, in whichever credential mode is present), table key schema, DP-INV-1 flag state, policy enforcement; prints the do-not-touch inventory. Ordinary-gateway digests are informational only | AWS credential (any mode) |
| `15-verify-isolation.sh` | **Proves** fixture isolation by opening real sockets from inside the fixture pods, rather than reading an applied NetworkPolicy back. Distinguishes REFUSED (reachable, nothing listening → *not* blocked) from TIMEOUT (silently dropped → the policy signature), and requires a positive control before any timeout counts as a pass. Read-only | AWS credential (any mode) + live fixture |
| `10-create-fixture.sh` | Creates the run-bound fixture: **NetworkPolicy first**, then dedicated FIFO queue, flag-ON gateway Deployment/Service, protected worker Job. Pins both images by **digest**. Records every resource in the ledger *before* creating it. `--check-only` server-side dry-runs and creates nothing. `--stage gateway\|worker\|all` selects which half runs; the worker stage requires `--edge-receipt` and admits the gateway stage's objects **by recorded uid**, never by name | AWS credential (any mode) |
| `lib/stage_gate.py` | Decides whether a stage may run, from the shared ledger plus the caller's observations. Separates *this run's object* (recorded, uid matches) from adoption, replacement, deletion-between-stages and an unreadable cluster. No cloud calls, no kubectl, no mutation — the caller observes, this decides. Also emits the gateway→edge handoff document | — |
| `20-collect-pause-evidence.sh` | Runs the real-SDK pause experiments (`control-runtime.integration.ts`) and assembles W2-03/04/05 artifacts. `--reuse <json>` re-assembles without re-spending model calls | model access (paid) |
| `21-assemble-pause-artifacts.py` | The assembler. Copies **measured values only**; writes `null` and names anything unmeasured | — |
| `22-collect-suite-evidence.sh` | Runs the neutral-contract suite per adapter and the four vocabulary suites; exports the stats schema from the Pydantic models. Produces W2-02/08/09 artifacts. `--no-cloud` skips the two deployed-digest booleans and records them **false**, never assumed true | none (`--no-cloud`) |
| `30-seed-and-count.sh` → `31-seed-and-count.py` | Seeds synthetic runs through the **real writers** and reads counters back from the **deployed reader**; snapshots before/after as isolated deltas on run-bound synthetic tenants. Produces W2-06/07 artifacts. `--check-only` validates and writes nothing | AWS credential (any mode) + admin token |
| `40-verify-edge-sessions.sh` | Verifies the three supplied identities are real and distinct *through the gateway*, and records the edge transport configuration read from Terraform. Sends **only** `Authorization` — never a forged `X-Caller-Identity`. With `--fixture-pod` the sessions run **inside the verified fixture** and are `fixture_scoped: true`; without it they reach the ordinary deployment and are recorded `false` | three session tokens |
| `lib/edge_sessions.py` | Session logic, unit-tested without a cluster. `fixture_scoped` is **derived** from the pod's own verified label + uid — an external URL can never earn it. Tokens go to the interpreter on **stdin**, never argv | — |
| `lib/probes.py` | Probe classification and the positive-control rule for `15-verify-isolation.sh` | — |
| `lib/experiment_binding.py` | Refuses `bypassPermissions` runs on a privileged host (fails closed), and refuses `--reuse` that would bind old output to a new revision/SDK/image | — |
| `90-cleanup-ledger.sh` | Bounded cleanup: synthetic rows by BOTH keys with consistent-read absence confirmation, ledger-named workloads, run-bound queue deletion; writes `cleanup-ledger-result.json` | AWS credential (any mode) |
| `artifact-templates/*.json` | Config and ledger templates, validated against the real `REQUIRED_CONFIG_FIELDS` | — |

`90-cleanup-ledger.sh` guard rails are covered by `tests/test_cleanup.py`: a row
missing `arrived_at` is refused before any delete, the pre-existing
`adp-dev-authority-probe-20260920.fifo` of unknown ownership is refused by name,
a `get-queue-url` error other than a distinguished NotFound is **not** read as
absence, and a successful `delete-queue` is not read as immediate absence.

## Runbook

Use the orchestrator. It owns the ordering, the run-id threading, the
stop-on-failure gates and — the part a human operator most reliably forgets under
pressure — the cleanup guarantee.

```bash
# Validate everything and create nothing. Safe to run while reviewing.
./run-all.sh

# Full run. Creates the fixture; tears it down on every exit path.
export W2_OWNER W2_NONOWNER W2_OTHER_TENANT W2_ADMIN   # values set by root
./run-all.sh --apply --gateway-url "$GW"

./run-all.sh --apply --no-paid        # skip the paid model step
./run-all.sh --apply --from 40        # resume from a step
./run-all.sh --apply --keep-fixture   # leave the fixture up (prints the teardown command)
```

### The staged path — required for a protected worker

A run that includes the **protected worker** is two invocations, not one, because
the sequence is genuinely circular otherwise: the worker needs a control endpoint,
that endpoint comes from #5836's fixture edge, and that edge is built in front of a
Service the fixture gateway stage creates. There is no order in which one invocation
can do both.

```bash
RUN_ID="w2-$(date -u +%Y%m%d-%H%M%S)"
EV="$HOME/w2-evidence/$RUN_ID"

# Stage 1. Creates policies, queue, gateway Deployment + Service; proves isolation.
#          Leaves the fixture RUNNING on purpose and exits NON-ZERO: the
#          worker-dependent checks were not attempted, so this is not a passing run.
./run-all.sh --apply --stage gateway --run-id "$RUN_ID" --evidence-dir "$EV"

# Read the next commands out of the handoff -- they already carry this run's nonce
# and the server-assigned uids. Do not retype the nonce: a typo produces a
# *plausible* wrong value, and the two runs then tag resources so neither
# teardown finds the other's.
cat "$EV/stage-handoff.json"

#   ... root runs #5836's create-fixture-alb.sh, the edge terraform apply and
#       fixture-lifecycle.sh handoff, then exports ALL of its outputs:
#         terraform output -json > "$EV/edge-outputs.json"
#       Not `-json ownership`, and not the ownership.json that apply writes to its
#       artifact dir: those carry the run bindings but NOT the control endpoint,
#       which #5836 publishes as a separate top-level output.

# Stage 2. Creates the protected worker against that receipt, runs the experiments,
#          and tears BOTH stages down from the shared ledger.
./run-all.sh --apply --stage worker --run-id "$RUN_ID" --evidence-dir "$EV" \
  --edge-receipt "$EV/edge-outputs.json"
```

Between the two invocations a **control-flag-ON fixture gateway and a live fixture
queue are running.** That is the unavoidable cost of the staged shape — the gateway
stage cannot tear down the Service the next stage's edge fronts — so `--stage
gateway` forces `--keep-fixture` rather than letting an operator forget it, and says
so loudly. If the sequence is abandoned midway, tear down from the ledger:

```bash
./90-cleanup-ledger.sh "$EV/cleanup-ledger.json" "$EV"
```

Both stages share one ledger and one nonce, so that teardown removes **both** stages'
resources by recorded uid. The worker stage reads the nonce and the queue URL from
the ledger rather than from flags.

Re-running a completed stage is refused, not adopted, and the refusal distinguishes
the two cases that look identical by name: an object **this run's ledger records with
the same uid** is its own earlier stage ("this stage has already run — continue with
the next stage"), while one the ledger does not record is a stranger's ("use a
different --run-id"). A replacement wearing the same name, a prerequisite deleted
between stages, and an unreadable cluster are each refused separately.

`--stage all` (the default) remains the one-invocation path and creates a worker only
when an `--edge-receipt` from an already-existing edge is supplied; without one it
builds a gateway-only fixture and the worker-dependent steps skip with that reason.

It exits non-zero if **any** step failed *or was skipped*: a skip is not a pass,
so a partially-skipped run must not look green. `orchestration-summary.txt` in the
evidence dir lists every step as PASS / FAIL / SKIP with a reason.

Cleanup runs from an `EXIT` trap registered before anything is created, so it also
fires on a gate failure and on Ctrl-C. If cleanup does not fully succeed it says so
loudly and tells you not to assume absence.

### Manual sequence (fallback / for reading)

What the orchestrator does, step by step:

```bash
RUN_ID="w2-$(date -u +%Y%m%d-%H%M%S)"
EV="$HOME/w2-evidence/$RUN_ID"; mkdir -p "$EV"
LEDGER="$EV/cleanup-ledger.json"

# 0. Target. Must exit 0 before anything is created.
./00-verify-target.sh

# 1. Fixture. Dry-run first, then create.
#    --stage gateway / --stage worker split this in two when a protected worker is
#    wanted (see "The staged path" above); plain invocation is --stage all, which is
#    a gateway-only fixture unless --edge-receipt is supplied.
./10-create-fixture.sh --run-id "$RUN_ID" --ledger "$LEDGER" --check-only
./10-create-fixture.sh --run-id "$RUN_ID" --ledger "$LEDGER"
#    prints gateway_url + deployed_digest for the harness config.

# 1b. PROVE isolation before any experiment. A control measurement taken through
#     an unverified boundary cannot be attributed to the software.
./15-verify-isolation.sh --run-id "$RUN_ID" --evidence-dir "$EV"

# 2. Suite/schema evidence (no AWS credential needed for the suites).
./22-collect-suite-evidence.sh --evidence-dir "$EV" \
  --ledger "$LEDGER" --expected-identity "$EV/expected-identity.json" \
  --gateway-image "$APPROVED_GATEWAY_IMAGE" --worker-image "$APPROVED_WORKER_IMAGE"

# 3. Real SDK pause evidence. Makes PAID model calls.
#    --expected-identity is REQUIRED on the live path and is the document 10- wrote
#    when it bound the worker pod. It is the only thing that ties the evidence to a
#    workload: without it the experiment can describe only itself, and a process
#    describing itself has established nothing about what was measured.
#    Its absence means 10- created no bound worker -- do not work around it.
./20-collect-pause-evidence.sh --evidence-dir "$EV" \
  --expected-identity "$EV/expected-identity.json" \
  --ledger "$EV/cleanup-ledger.json"

# 4. Identities. Root exports three real session tokens first (see below).
#    --fixture-pod makes the sessions fixture-scoped; omit it and the result is
#    recorded fixture_scoped: false because the URL reaches the ordinary pods.
export W2_OWNER W2_NONOWNER W2_OTHER_TENANT      # values set by root, not here
FX_POD="$(kubectl get pods -n adp-gateway -l "adp.io/w2-fixture=$RUN_ID" \
  --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')"
./40-verify-edge-sessions.sh --evidence-dir "$EV" --run-id "$RUN_ID" \
  --fixture-pod "$FX_POD" --gateway-url "$GW" \
  --owner-token-env W2_OWNER --nonowner-token-env W2_NONOWNER \
  --other-tenant-token-env W2_OTHER_TENANT

# 5. Seed + count. --check-only validates everything and writes nothing.
export W2_ADMIN                                   # platform-admin bearer token
./30-seed-and-count.sh --run-id "$RUN_ID" --ledger "$LEDGER" \
  --evidence-dir "$EV" --gateway-url "$GW" --admin-token-env W2_ADMIN --check-only
./30-seed-and-count.sh --run-id "$RUN_ID" --ledger "$LEDGER" \
  --evidence-dir "$EV" --gateway-url "$GW" --admin-token-env W2_ADMIN \
  --owner-user-id "$OWNER_USER_ID" --owner-tenant-id "$OWNER_TENANT_ID"

# 6. Harness. All ten checks and verified cleanup are required for acceptance.
python3 ../../agent-control-eval.py --wave 2 --config "$EV/fixture-config.json"

# 7. ALWAYS, including on failure, after evidence is preserved.
./90-cleanup-ledger.sh "$LEDGER" "$EV"
```

## What root must supply at execution time

Four things, none of which a script can produce:

1. **An AWS credential that resolves to `879318057152`** — in any of the three
   supported modes (below). Not specifically a vault session: root has valid
   embark1/instance credentials but no `adp-cred` binary, and requiring one
   made step 00 unrunnable on the host that has to run it.
2. **Three real session tokens** (`owner`, `nonowner`, `other_tenant`). Minting is
   root's step because it needs pool-admin credentials the eval scripts must not
   hold — **not** because it needs a browser: `src/auth/cli_login.py` mints
   non-interactively on the CLI app client (`AdminSetUserPassword` +
   `ADMIN_USER_PASSWORD_AUTH`, the same mechanism the github-auth-broker uses on
   every sign-in), and `src/auth/cli_native_login.py` covers native-password users.
   So disposable sessions can be supplied without creating live identities. An
   earlier revision of this list claimed interactive OAuth was the only flow; root
   corrected it. `nonowner` must be a different user in the **same** tenant as `owner`;
   `other_tenant` must be in a **different** tenant. With fewer than three, a 404
   for a foreign row is indistinguishable from a missing row.
3. **A platform-admin bearer token** for `/admin/agent-run-stats`. An *org* admin
   token is silently scoped to its own org — `31-seed-and-count.py` detects that
   and fails loudly rather than reporting every delta as 0.
4. **Model access** for step 3, which makes paid Claude calls. There is no
   substitute: unit mocks are explicitly not acceptable for W2-03/04/05.

## Remaining gaps

Each is named, owned, and **not** worked around.

**G2 — W2-01 and W2-10 have no predicate in the harness.**
`WAVE2_PREDICATES` contains only W2-02…W2-09; W2-01/W2-10 sit in
`PENDING_CHECK_OWNERS` and emit `not_run`. Since `required` stays at 10, the gate
`passed == required and not_run == 0` is unsatisfiable and `--wave 2` exits 4
regardless of fixture quality. **The best obtainable result today is 8 passed /
2 not run, exit 4.** Confirmed by evaluating the harness's own
`report_is_passing` on both outcomes: `{8,0,0,2}` → `False`, exit 4; `{10,0,0,0}`
→ `True`, exit 0. So the fixture is complete and the ceiling is purely the two
absent predicates. *Owner: defect #5825, actively implementing them. Do not
implement them here and do not re-prove this gap.*

**G6 — W2-05 has a genuine measurement gap.** `21-assemble-pause-artifacts.py`
populates W2-03 and W2-04 entirely from real recorded measurements (verified: all
9 and all 11 required keys non-null). W2-05 needs twelve fields no current
experiment measures — `auto_resumed`, `annotation_count`, `extra_assistant_turn`,
`neutral_annotation`, `resolved_before_release`, `pod_killed`, `idle_retry_fired`,
`exit_watchdog_fired`, `heartbeats_during_pause`,
`paused_distinguishable_from_stalled`, `deadline_clamp`, `cancellation`. They are
written `null` and named on stderr, so W2-05 fails honestly rather than passing on
a default. Closing it needs a shortened-pause-budget expiry experiment added to
`control-runtime.integration.ts`. *Owner: S2 #3961.*

**G7 — `harness_neutrality.native_interrupt_status` is unmeasured.** It is the
status a run reaches after a native interruption with *no* confirmed ADP abort
finalization, which needs an instrumented run rather than a seeded row. Recorded
`null`. **The harness tests it with `== "aborted"` only, so null satisfies that
sub-check vacuously** — the artifact says so in `_provenance`, and it must not be
reported as verified.

**G8 — the protected worker bootstrap is not provisioned.**
`agent-authority-enabled` is `false` and the approved-digest allowlist is empty,
so the fixture worker's projected `adp-agent-bootstrap` token cannot be
exchanged and no worker-originated control command can be authorized.
The default `10-create-fixture.sh` run reports this and continues; the pause
evidence comes from `20-collect-pause-evidence.sh`, which does not depend on the
bootstrap. Enabling it is a Terraform platform change
(`agent-authority-bootstrap.tf`) — root's decision, and explicitly not a DP-INV-1
flag flip.

Note how this meets `--worker-job`: on an environment whose allowlist is still
empty, that flag **refuses before creating anything**, because `digest in []` is
false for every digest and a worker rendered against an empty allowlist could
never complete a bootstrap. An empty allowlist admits nothing, so it must not be
read as admitting everything. The refusal names the Terraform-owned variable
(`agent_authority_worker_image_digests`) rather than working around it.

**Fixed this run, noted so review does not re-derive them:** the config template
named only the `owner` identity, but the harness resolves all three of
`owner`/`nonowner`/`other_tenant` and raises `PrerequisiteMissingError` on any
absent one — the template would have failed at the first identity check. And in
three scripts the post-run `rc=$?` sat under `set -e`, so the failure branch
carrying the operator's next step (cleanup command, session-minting
instructions) was unreachable; `set -e` is now suspended around those calls and
the exit status still propagates.

**Documentation drift (non-blocking):** the runbook says "five checks remain
pending" and lists W2-03…W2-05 as pending, but the code implements them — only
two are. `docs/runbooks/agent-control-evaluation.md` ~lines 429/444. In #5825's
stated scope.

## Per-check readiness

| Check | Status | Notes |
|---|---|---|
| W2-01 | blocked, G2 | No predicate. Fixture digests *are* pinned at creation |
| W2-02 | ready | `22-collect-suite-evidence.sh`; both adapters, real per-adapter test counts |
| W2-03 | ready | `20`/`21`; all 9 keys populated from real measurements |
| W2-04 | ready | `20`/`21`; all 11 keys populated from real measurements |
| W2-05 | blocked, G6 | 12 fields unmeasured, written null and named |
| W2-06 | ready\* | `30-seed-and-count.sh` with `--owner-tenant-id`; needs a real owner session |
| W2-07 | ready | Isolated deltas on three run-bound synthetic tenants |
| W2-08 | ready | Suites + both deployed digests (omit `--no-cloud`) |
| W2-09 | ready | Schema exported from the Pydantic models; live call needs a session |
| W2-10 | blocked, G2 | No predicate. `90-cleanup-ledger.sh` supplies the observations |

Suite-path note for W2-08: the harness key `tests/test_status_vocabulary.py` is
**module-relative** and resolves to
`modules/agent-factory/agent-worker-image/tests/test_status_vocabulary.py`. Not a
defect; the file exists there.

## Design rules these scripts hold to

* **Policy before listeners.** There is never a window where the fixture gateway
  is reachable but unprotected.
* **Digest pins, never tags.** ECR tag mutability does not weaken a digest pin.
* **Ledger before creation.** A ledger entry for a resource that failed to create
  is harmless; a created resource missing from the ledger is a leak.
* **Both key halves, always.** Rows are deleted on `event_id` AND `arrived_at`; a
  partial-key delete could match an unrelated item.
* **Deltas, never shared totals.** The table holds ~391k rows across live
  tenants.
* **Measured or null.** No script ever writes a default that happens to be the
  value a check wants to see. Unmeasured fields are null and named on stderr.
* **Real writers and real readers.** Rows are written by production code and
  counted by the deployed reader, so no artifact can agree with itself.
* **No forged provenance.** Nothing sets `X-Caller-Identity` or
  `X-Adp-Edge-Provenance`. On AWS_IAM routes API Gateway overwrites both from the
  verified SigV4 principal; on auth-NONE routes it blanks them. A forged header
  yields 403 and proves nothing.

## Do not touch

`authority-probe-gateway-20260920` (deployment) and
`adp-dev-authority-probe-20260920.fifo` — unknown ownership, per the assignment:
do not reuse, mutate or remove. `10-create-fixture.sh` and `90-cleanup-ledger.sh`
refuse them **by name**, and `00-verify-target.sh` reports them in its
do-not-touch inventory if present. Covered by `tests/test_cleanup.py`, so the
refusal is a tested behaviour rather than an operator convention. Also: no ECR mutability change, no
shared source-role retirement, no broad flag activation, no ordinary route
changes.

### Authenticated SDK worker handoff

The evaluation worker image can run the SDK harness after normal task acquisition
and protected bootstrap. Its trusted envelope must include `payload.control_evaluation`
with `run_id`, `run_nonce`, the full `source_revision`, and the SHA-256 of a Git
bundle containing that revision. Prepare the bundle **before** provisioning and
publishing that immutable dispatch. The fixture's gateway must allow the verified
worker image digest, and the worker must use the fixture edge for both control
and model calls.

After creating and recording the worker Job, use `worker_observation.py` through
`10-create-fixture.sh` to record its real pod UID and resolved image. Then run:

```sh
KUBECONFIG="$OPERATOR_KUBECONFIG" python3 \
  platform/scripts/operator/wave2/lib/evaluation_handoff.py \
  --envelope "$EV/protected-envelope.json" \
  --ledger "$EV/cleanup-ledger.json" \
  --identity "$EV/expected-identity.json" \
  --bundle "$EV/source.bundle" \
  --output "$EV/worker-collection"
```

This helper requires the worker's `bootstrap-ready.json` to identify the acquired
invocation. It transfers the source and observation documents, then creates `ready`
last to start the experiment. It checks the pod's ownership and UID during polling,
collects the result, logs and evidence into a private directory, and creates
`collected` only after collection. A nonzero SDK result remains a failure.
It does not create authority, enqueue a task, create a worker, or establish live
acceptance merely by completing a transfer. Do not rerun a started handoff: retain
its existing files and collect them before the worker's collection timeout.

### Vocabulary deployment evidence

The suite collector requires the fixture ledger, operator-observed worker identity,
and independently approved gateway/worker digest references. Its deployment probe
reads the gateway Deployment → ReplicaSet → Pod ownership chain and the exact
worker Pod/Job UIDs. It compares resolved runtime image IDs with those approved
references. An ordinary ScaledJob template, a digest-shaped string, an unready pod
or a replacement pod cannot establish deployment. Collect while the fixture pods
are running. `--no-cloud` records both deployment verdicts false; it cannot pass
W2-08. Set `W2_KUBECONFIG` for an existing operator kubeconfig. Profile, instance
role and vault modes use the same target-account check.

### Completing the external pod-survival observation

The SDK cannot observe Kubernetes restarts or eviction of its own pod. After
collecting its result, rerun `21-assemble-pause-artifacts.py` with the same `--raw`
and `--pod-observation <operator-observation.json>`. This assembles evidence only;
it does not repeat model calls. The observation document contains:

- `before`: the Kubernetes Pod JSON captured before the source handoff.
- `after`: an operator observation with `pod_uid`, `returncode`, `observed_at`,
  optional `deletion_timestamp`, and the actual Kubernetes `status` object.
- `result`: the collected worker result, including its `pod_uid`.
- `result_collected_at`: the operator's timestamp for receiving that result.
- `events`: the Kubernetes EventList selected by the same pod UID.

The post-experiment observation must follow result collection. The assembler
compares container IDs and restart counts, checks termination against collection
time, and rejects foreign events and missing observations. A collector exit after
the experiments is distinct from a worker dying during them. The derived artifact
retains the observation path and hash; missing inputs remain incomplete.

### Registered controls and native interruption (#5891)

Set `payload.control_evaluation.mode` **before signing/publishing the dispatch**:

| Mode | Execution | Required observation |
| --- | --- | --- |
| `sdk` (default) | Existing SDK experiment suite | Existing Wave 2 evidence |
| `registered-control` | Production runtime factory and real `resilientQuery` with foreground tool work | Live gateway state/commands and browser capture of this invocation |
| `native-interrupt` | Same factory/query; calls the active SDK handle's `interrupt()` after an assistant turn is observed | Native request/ack trace and this invocation's non-aborted terminal row |

Registered modes require successful production registration. A missing token is a
failure, with no fallback to an unregistered SDK experiment. The launcher uses the
exact dispatched source bundle, installs its lockfile dependencies and starts
`registered-control-fixture.ts`. It limits the SDK to one attempt, 300 turns and a
15-minute runtime deadline. The parent also bounds subprocess lifetime and kills
its process group on exit. Use disposable fixture infrastructure and foreground
work only; keep ordinary control flags and allowlists unchanged.

The registered fixture atomically writes `<output>.progress.json` with mode `0600`
as runtime events occur. It records invocation/source/generation, SDK query
attachments, cumulative positive changes in active tool admissions, current
active work, and an observation sequence. These are local measurement inputs for
before/after rejection probes; they do not establish rejection or acceptance by
themselves. Unknown activity makes `counters_complete` false permanently. Reject
incomplete counters or dropped events, and obtain accepted-command counts from
the actual listener journal. The snapshot contains no credentials or SDK content.

Use the authenticated handoff command above for each new invocation. While
`registered-control` runs, collect live command responses and browser actions
against that invocation. Use a separate invocation for abort so it cannot end the
pause/resume/steer observation early. The collection helper preserves
`registered-runtime.json`, verifies its dispatch identity, and signals `collected`
so the entrypoint can finalize the same invocation. A nonzero collection exit
retains all available evidence; it does not authorize dispatching a replacement.

After collection, read the terminal result through the fixture gateway:

```sh
python3 platform/scripts/operator/wave2/lib/registered_runtime.py \
  --runtime "$EV/worker-collection/registered-runtime.json" \
  --envelope "$EV/protected-envelope.json" \
  --session-file "$OWNER_SESSION_FILE" \
  --gateway-url "$FIXTURE_GATEWAY_URL" \
  --out "$EV/registered-terminal.json" --timeout 180
```

The session file stays private. The helper makes authenticated read requests only,
rejects foreign invocation/source/mode, missing lifecycle events, failed cleanup,
lost events and inconsistent terminal outcomes. A native SDK interruption may
finish as `failed` with exit code 1; that preserves the actual SDK outcome. It must
never become `aborted` without a gateway-authorized operator abort. A local trace
or a successful subprocess exit alone cannot establish live acceptance. Missing
terminal rows time out rather than being synthesized.

Retain the pod UID, Job UID, resolved worker digest, source/build receipt, gateway
digest, command/browser captures and fixture ledger alongside these reports. Use
the existing ledger cleanup only after worker drain and evidence collection;
verify the exact owned resources are absent. LF-01/03/04 have local production-path
regressions; LF-02/03/05 still require the maintainer's actual live observations.

### Control transport CIDRs

Before enabling fixture controls, pass `--cluster-pod-cidrs` to
`10-create-fixture.sh` using verified private pod ranges from the target cluster.
The renderer can also retain an explicit inline
`AGENT_CONTROL_CLUSTER_POD_CIDRS` on the source Deployment. An `envFrom` reference
alone cannot establish that this setting exists; missing or invalid ranges stop
rendering. Empty configuration makes the gateway refuse live control transport
with HTTP 409 even when registration and capability flags succeed.

For maintenance of an existing isolated fixture, an observed owned worker IP as
`/32` is sufficient for that worker. Verify its pod UID before applying the
fixture-only setting, and update it for subsequent workers. This setting does
not replace the fixture NetworkPolicies or authorize changes to ordinary controls.


## Cleanup policy scope

When a run includes temporary isolation canaries as well as later workers, retain
all resources in the creation ledger. An optional `selection_observation` on a
NetworkPolicy entry records its actual Kubernetes object as `{command,
retrieved_at, body}`. Supply the same observation on every Pod and Deployment
entry checked against it. Object kind, name, UID and namespace must match the
ledger; deployment selection uses pod-template labels. The evaluator applies
Kubernetes namespace and label-selector semantics before checking that a selected
workload ended before the policy was removed. Canary policies therefore constrain
the canary pods they selected, including their required deletion order.

Missing policy scope preserves the previous conservative ordering against every
workload. Supplying policy scope without workload observations fails; unknown or
malformed selectors cannot exempt resources. These observations do not establish
that anything was deleted: independent removal receipts and absence checks remain
required for every ledger entry, including canaries removed earlier.
