# Learnings — issue #5684 (A19: controller ClusterRole reads every Secret in the cluster)

2026-09-21 AWS security scan, work package A19; parent #5677, daily epic #5599. One finding:
`src/superplane-controller/deploy/controller.yaml` granted `secrets: ["list"]` cluster-wide.

Outcome: the grant and the check it fed were removed together; a second, live, previously
unreported defect was found in the same rule set (`pods/eviction` under an apiGroup that can
never match, silently breaking every node drain); verbs on seven other resources narrowed to
traced call sites; RBAC contract tests added in both directions, mutation-tested.

## What generalises to other permission-scoping work

**Ask what the permission is *for* before deciding how to narrow it.** The obvious reading of
this finding is "scope the Secret grant down" — to a namespace, to names, to one label. The
actual answer was that the grant had no legitimate consumer at all: its only caller listed
every labelled Secret and then returned the constant `"synced"` on every branch, never reading
a single field of what it fetched. The broadest permission the controller held funded an answer
that was already a constant. Narrowing it would have produced a smaller grant serving an
equally fake signal, and the finding would have closed. Trace the call site first; sometimes
the minimal grant is no grant.

**Removing a permission whose caller still runs can make things worse, not better.** Had I
deleted only the RBAC rule, the `List` would fail with a permission error and the function
would *still* have returned `"synced"` — converting a dead signal into a guaranteed lie, and
one that now fires precisely when something is wrong. The grant and its consumer had to go in
the same change. Whenever a least-privilege edit removes a permission, check what the code does
on the resulting error path; "fails closed" is an assumption, not a default.

**Check what the receiver does with the value before preserving the field for compatibility.**
My first instinct was to keep `vault_sync_status` and send `""` to avoid breaking the consumer.
Reading the consumer (`superplane-platform-monitor/monitors/cluster_health.go`) showed that
wrong twice over: `"synced"` was never in its `ok|pending|failed` vocabulary, so the field was
already being discarded as uninterpretable at the far end — and an *absent* field hits its
`case ""` → NotChecked, which is the honest "nothing was reported", whereas a present-but-empty
value aggregates as Unknown and implies a reading was attempted. Omitting the field was both
the smaller change and the more truthful one. The domain's health contract
(`contracts/superplane_contracts/health.py`) already cited this function by name as its
motivating bug, which corroborated the finding from a direction the scanner never looked.

**Audit the whole rule set, not just the flagged rule.** The scanner reported the Secret grant.
Reading every neighbouring rule against its call sites found something worse and unreported:
`pods/eviction` was granted under `apiGroups: ["policy"]`. RBAC matches a subresource against
the API group in the **request path** — `POST /api/v1/namespaces/{ns}/pods/{name}/eviction`,
the core group — not against the `policy/v1` apiVersion that the Eviction *body* carries. A
mismatched group is not a validation error: the rule loads cleanly and simply never matches, so
every drain is refused 403 at the moment it runs, bypassing the PodDisruptionBudgets that
`RETIREMENT.md` documents drain as respecting. A security package that only closes its assigned
finding leaves this class in place, because nothing else reads an apiGroup: no lint, no schema,
no unit test, and the failure needs a live cluster and a real drain to appear.

**Verify an RBAC claim against upstream's own bootstrap policy, not against memory.** I checked
the eviction group against `plugin/pkg/auth/authorizer/rbac/bootstrappolicy/policy.go`, where
`editRules()` and `NodeRules()` both use the legacy (core) group for `pods/eviction`, and
against cluster-autoscaler and Karpenter. My first guess at *which* upstream roles carry the
grant (node-controller, disruption-controller) was simply wrong — they have none. Guessing the
corroborating source is as risky as guessing the fact.

**Least privilege has a failure mode in the other direction, and it is quieter.** An over-broad
rule works — too well, silently. An over-narrow rule also applies silently and fails later, in
the operation that needs it. For this controller that operation is a node drain, so the
symptom is capacity that will not release, surfacing as cost rather than as an error anyone
attributes to RBAC. Every verb I removed therefore needed a traced reason it is never
exercised, not merely an absence of evidence that it is:

- `configmaps` (8 verbs, commented "for leader election") — controller-runtime v0.20.1 defaults
  to `resourcelock.LeasesResourceLock`; the ConfigMap lock was the pre-v0.12 default and nothing
  selects it. The Deployment's own `configMap` mounts and `configMapKeyRef` env are resolved by
  the **kubelet**, not by this ServiceAccount, so they survive the removal — worth confirming,
  since the manifest visibly mounts ConfigMaps and that looks like a contradiction.
- `leases` list/watch/patch/delete — client-go's `LeaseLock` calls exactly Get, Create, Update
  (release rewrites the holder via Update), and uses a direct client, so no watch is implied.
- `nodes` patch — cordon is a read-modify-Update.
- `superplanenodes` delete — nothing deletes one, and `RETIREMENT.md` *requires* a failed or
  unconfirmed teardown to retain the record and its `status.skypilotCluster`, because that name
  is the only handle on a possibly-live GPU cluster. Here the missing verb protects a safety
  property, not just an attack surface.
- `/status` get/patch — `Status().Update` issues a PUT.

**A "read" verb on a cached client is really two.** controller-runtime's cached client backs
every Get with an informer ListWatch, so trimming `list`/`watch` off a resource the controller
only ever `Get`s breaks the cache at startup. This is an easy and invisible over-narrowing.

**`resourceNames` does not do what "scope it to specific objects" suggests — and the accurate
reason matters.** I initially believed it cannot restrict `list`/`watch` at all. It can, but
only when the client sends a matching `metadata.name` field selector; without one,
`requestInfo.Name` is empty, nothing equals it, and the rule does not match — so the call is
*denied*, never authorised-but-unfiltered. (It genuinely never applies to `deletecollection` or
top-level `create`.) For this controller the reads are the opposite pattern by design: an
unschedulable pod is found precisely by *not* knowing which pod it is, and auto-repair creates
records with `generateName`, so the name does not exist until after the call that would need
authorising for it. The correct scoping was therefore the *absence* of `resourceNames` — which
looks like an oversight unless the reasoning is written down where the next reviewer will trip
over it. The issue's own wording ("without pretending `resourceNames` can filter unsupported
list patterns") was a real constraint, not boilerplate. I had the right conclusion from the
wrong premise, and fixed the premise rather than keeping a test whose stated reason was false.

## Testing

**Pin both directions or the test is half a guard.** "No broad grants" passes happily against a
role trimmed until eviction no longer works. So the manifest suite asserts the removals *and*
the 16 grants that named call sites justify, each carrying the call that proves it, so a future
trim fails in CI rather than during a drain.

**A manifest test cannot see the code drifting away from the manifest.** If someone later writes
a call needing a removed permission, the manifest suite keeps passing — the role is still
narrow, it is now narrow in the *wrong shape* — and the mismatch surfaces as a 403 in a live
reconcile loop. That needed a second suite, in Go, watching the source for the operations whose
grants were removed: Secret reads, ConfigMap access, SuperplaneNode deletion, vault-sync
reintroduction, and that drain still goes through `SubResource("eviction")` rather than a plain
pod `Delete` that would bypass PDBs. The removals are only safe while those premises hold, so
the premises are what get tested. (Same lesson as S01's no-subprocess check, reached from a
different finding — which suggests it is the general shape for "the fix is a deletion".)

**Assert on parsed values, not raw text, when the artifact documents itself.** The manifest now
explains the removal at length and therefore *says* `secrets`. A substring scan would fail on
the explanation it exists to protect, and the cheapest way to pass would be deleting the
reasoning — leaving a file that no longer records why the grant is absent. Same for the Go
tests, which strip comments before scanning; the restored-baseline run proved this works, since
the fixed `heartbeat.go` mentions `checkVaultSyncStatus` throughout its explanatory block and
still passes.

**Mutation-test, and make the negative control a real prior state.** I restored the pre-A19
manifest from the branch's own base commit and re-ran: 10 failed / 18 passed, covering secrets,
the eviction apiGroup, the removed grants, status verbs and the comment requirement. Same for
the Go side against the pre-A19 heartbeat sources. Using the actual previous revision rather
than a hand-edited fake means the guard is proven against the exact regression it is written
for.

**Require each rule to carry a justification, as a test.** The cluster-wide Secret grant shipped
under the comment "Secrets: list for vault sync status check (heartbeat)" — which named a caller
but not the fact that the caller ignored the result. That is how it survived review: an
unannotated (or vaguely annotated) rule is indistinguishable from an inherited one, so the next
reviewer cannot tell a deliberate grant from a leftover without re-deriving the whole file,
which is exactly what did not happen. The structural check ("every rule is preceded by a
comment") is crude but it makes the omission impossible to repeat silently.

## Toolchain / environment

**Run the linter version CI pins, not the one that installs by default.** `ruff` 0.16.8 reported
176 findings across the module and a reformat of my file; CI pins `ruff==0.9.6` via
`modules/gateway/pyproject.toml`, under which the module is clean and only my file needed
formatting. Had I "fixed" the 0.16.8 findings I would have produced a large unrelated diff that
still would not have matched the gate. Also note the module's `.ruff.toml` is standalone
(`extend-exclude = ["src"]`, default ruleset) — the transferred `src/` tree is deliberately
excluded to preserve its byte-fidelity audit, so the Go component's own tests are what gate it.

**Let the formatter win on layout, but restructure rather than accept the ugly output.** 0.9.6
exploded my `(group, resource, verb)` dict keys into five-line blocks. Rewriting the *values* as
parenthesised strings gave a stable formatting fixpoint that is also readable, instead of
committing something the formatter merely tolerated.

**Distinguish a sandbox gap from a regression before reporting either.** The module suite hit
three missing dependencies (`asyncpg`, `pgserver`, `alembic`) that CI installs in its own steps.
`git log` showed the postgres lane arrived with PR #5598 and is untouched by this branch;
installing `alembic` turned the one apparent failure into 30 passed. Final state: 4038 passed,
11 skipped, with only the embedded-Postgres lane un-runnable here and named as such. Reporting
"1 failed" without that check would have implied a regression I did not cause; silently
ignoring it would have hidden a real one.

## Scope discipline and honesty about what this changes

**Reconcile with the overlapping PR before editing, as instructed.** PR #5598 (controller
management) is merged; its management mode authenticates with its own kubeconfig token and its
installer-rendered manifests ship no ClusterRole at all (asserted by the installer's own test).
No overlapping permission surface, so no conflict and nothing to coordinate — but that had to be
established rather than assumed, and it is cheap to check.

**Say plainly that this manifest is not a live deploy path.** `src/*/deploy/` is audited
*inventory* of what upstream ships (it still carries upstream account `605440105851` and
`:latest` tags); ADP's applied manifests live in `k8s/` and `infra/control-plane/`, and
`tests/test_transferred_source_boundary.py` asserts no workflow reaches into `src/`. So merging
this narrows no live cluster's permissions on its own — a deployment owner has to reconcile it.
Per the issue's exclusions I applied no RBAC, rotated nothing and ran no paid scans. A security
fix that reads as "the hole is closed" when it closes the hole only in inventory is the kind of
overclaim that stops the real remediation from being scheduled, so the PR says this explicitly
rather than in a footnote.
