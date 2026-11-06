# Flow authoring validation — 2026-10-27

These are documentation/plan checks, not POC implementation or runtime results.
The table below records checks before inert registration; the later registration
results follow it. The three evaluation issues are
native children of #8887 and carry only the `evaluation` label; no persona was
triggered. The original 13 feature stories and native dependency sets are retained.

| Check | Result |
|---|---|
| Current repository `LoopProposal` schema and graph validator | 18 nodes, 42 edges, three waves; valid. Three machine evaluations, explicit code-only nodes, inert proposed policy and ordered repeated issue identities. |
| Canonical acceptance/dependency mapping | 13 feature stories, all 44 AC/SCN pairs and ten ECs preserved. Build/acceptance nodes are distinct; every same-wave story has a direct evaluation edge. |
| Planned files against immutable authoring revision | 135 unique files: 106 proposed creations, 29 existing modifications, 14 subsequent edits to predecessor-created files. No duplicate creators, missing existing paths or unordered writers. |
| Canonical design/source references | 269 local links and 130 pinned source links resolve; combined native/capability graph is acyclic. One unchanged external driver link requires separate HTTPS verification and is not counted as a new check. |
| Feature/epic issue preparation | Stable AC/SCN/EC IDs retained; S08/EC-07 explicitly clarify launch-delegated recommendation review and preaccepted cleanup. Other feature Validation sections remain unchanged; all bodies fit 8,192 bytes. Evaluation issues follow the run-only template and mandatory defect protocol. |
| Frozen ADP inventory | Reproduced 12,678 tracked artifacts, 3,730 entry candidates, 520 DB candidates and 160 migrations at the architecture's pinned ADP revision; no inventory differences. |
| Public documentation | The Engineering Insights consolidation checked 1,881 tracked documentation files with zero findings. |
| Unattended policy | Zero authored/human evaluation gates; native machine evidence; generic v2 delivery contracts; 106 explicit operations repair paths and required CI. |
| Negative evidence checks | Native predicates reject fail/skipped/not-run/null/missing outcomes for all 44 ACs and ten ECs. |
| Private launch materializer | Synthetic bound proposal passes native validation; seven invalid binding/expiry cases rejected. CLI writes mode 0600, preserves inert policy, refuses overwrites and repository output. No real environment binding was inferred or tested. |
| Deployment scope | One planned release campaign, zero per-story deployments. Cleanup is separate; flag transitions, required upgrade/rollback and genuine repair rollouts remain explicit. |

Reproduce with a Python environment containing the gateway plan-validator
dependencies (Pydantic 2, SQLAlchemy and PyYAML):

```sh
python3 docs/engineering-insights/devlake-poc/delivery-flow/validate_plan.py
python3 docs/engineering-insights/devlake-poc/validate_design.py
python3 scripts/check-public-docs.py
git diff --check
```

The full source-link check adds `--upstream-root` pointing to a local checkout
containing the pinned Apache DevLake commit. It reads Git objects; the checkout's
working HEAD is not substituted for that pin. No application regression suite
was run for this documentation change. Private launch admission and actual actor/runner qualification, materialized generic
delivery bindings, S11 generated live field inventory, proposed Go extraction/build,
Terraform plans, AWS resources, providers and all 44 feature ACs remain unverified
until implementation and the acceptance campaign.

The unattended amendment follows the explicit owner request. It removes routine
human checkpoints without claiming a live qualification. The existing self-hosted
runner pool must support bounded jobs with fresh credentials and the engine/workflow
must support the 56-hour campaign window, and S07 must prove actual repair,
continuation and closure paths before runtime dependence. The public plan remains
unresolved and inactive; schema checks are not proof of those external facts.

The POC architecture and flow are now co-located under
`docs/engineering-insights/devlake-poc/`. The immutable `spec_revision` names
the relocation commit so existing-file checks use the canonical paths. Historical
source pins and all seven frozen inventory files remain unchanged. The relocated
launch materializer also passes a synthetic CLI check from outside the repository,
preserving inert output, mode 0600 and overwrite/repository-output rejection.

## Parallel registration checks — 2026-10-27

- The refreshed baseline preserves all 135 planned files, 44 AC/SCN pairs and
  ten ECs. The proposed action limit is two. Shared CLI preflight belongs to S09
  CI/S11 integration; shared-capability and runtime locking checks belong to S07.
- Native proposal validation passes. Native acceptance-gate insertion also passes:
  19 nodes, 43 edges, one initial gate and all three unchanged machine evaluation
  specifications. The initial evaluator uses the same unannotated outer shape as
  existing running L5 plans; mandatory merge/CI evidence remains in its specification.
- Synthetic launch materialization followed by the actual dispatcher input resolver
  accepts valid repository/connection scopes and rejects every revoked connection
  prerequisite. No issue-number pseudo-repository or readiness-document pseudo-
  connection is used as a supposedly executable dependency.
- Design/reference validation passes with 276 local and 130 pinned source links.
  All existing story acceptance/scenario rows remain unchanged.
- ADP initially refused the redundant first-wave annotation, then temporarily
  reported GitHub planning-reference verification unavailable. The corrected
  proposal registered on retry with the original Product, Project, repository and
  epic bindings retained. No verification scope or authorization was removed.
- The registered draft has version 1 and one unanswered initial acceptance gate.
  No active execution policy, worker dispatch, cloud deployment or live POC
  acceptance is claimed. Actual IDs, policy hash and receipts remain private.

The server-saved graph and policy are the runtime source of truth. The dated
registration instructions above required all launch inputs before code acceptance;
the correction below separates independent code admission from live admission.

## Approved-flow startup correction — 2026-10-28

The live decision log recorded initial approval and a subsequent resume, but S01
had zero attempts. Its required input was
`environment_connection:unresolved-poc-connection`, with no connection grant in
the accepted policy. The dispatcher correctly refused that input. Every build
story depended on S01, so no implementation could start. Approval or resume alone
could not resolve the placeholder.

The correction makes S01 consume the repository design and record the scope,
field map, measurement plan and explicit readiness gaps. It preserves all graph
edges, acceptance criteria, planned files, machine evaluations, limits and
operations connection checks. Code merge does not close live acceptance.

- Native schema and transformed initial-gate validation pass.
- The actual input resolver now admits code inputs with an empty connection
  grant, before materialization; both operations still refuse unbound inputs.
  Bound-input and revoked-connection checks also pass.
- All 44 AC/SCN pairs, ten ECs and 135 planned files remain intact; all 54
  mandatory predicate negative cases pass.
- Design references: 276 local links and 130 pinned source links valid.
- Public documentation: 1,956 files checked, zero findings; `git diff --check`
  passes.

These checks establish the correction's admission behavior, not live execution
or deployment. Read the private flow graph and execution ledger after applying
the native amendment to confirm actual worker dispatch. Runtime bindings remain
unresolved and must be verified before the live campaign.
