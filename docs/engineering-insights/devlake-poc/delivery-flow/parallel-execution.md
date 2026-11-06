# Registration and parallel execution

Register this POC alongside existing flows with a proposed limit of **two
concurrent actions**. Keep its three waves, 13 feature issues, three machine
evaluations, 44 AC/SCN pairs and one release campaign. The public plan and file
inventory use the refreshed immutable source baseline in `spec_revision`.

## Registration is separate from launch

Read the current tenant, Product/Project registry and repository installation.
Bind only identities verified through those APIs in a private copy of the
proposal. Runtime connection, target, resource and launch-manifest sentinels may
remain unresolved in this inert draft. Do not borrow those values from another
flow. Keep private configuration and the resulting registration receipt outside
the repository.

Run `adp flow create --file PRIVATE_PROPOSAL --json` without `--yes`. The CLI
previews, registers and reads back an inert draft; exit 4 means it awaits the
initial acceptance decision. Read back the effective graph, proposed policy,
repository identity, Product/Project binding and inserted acceptance gate. The
flow must have no running nodes or active execution policy. If registration
reports an error, inspect its receipt before retrying the same proposal.

The first code evaluator uses the native machine specification directly, as the
existing running L5 flows do. Its optional top-level `trusted-evaluation`
annotation is omitted because the installed validator otherwise classifies the
inserted initial acceptance gate as a same-wave result gate. All predecessor
merge checks, required CI, evaluator policy and evidence contracts remain intact.
The package validator checks the transformed 19-node, 43-edge registration graph,
including the initial gate, as well as the 18-node authored proposal.

Initial acceptance can start repository code work with no environment connection
grant. S01 reads the repository design and records unresolved launch facts; it
must not require a placeholder connection. Complete the
[launch contract](launch-contract.json) before live operations. If the flow is
already accepted, bind verified runtime values through a native amendment of
its current plan; preserve completed gates, work, evidence and existing limits.
Creation alone grants no authority, and code approval grants no missing cloud
connection. Keep one planned initial gate and no per-story deployment gates.

## Source and integration ordering

The 2026-10-27 review found five running flows and two queued. Among the running
flows' declared remaining file plans, the shared CLI contract overlaps Security
[PR #9132](https://github.com/aws-e/adp/pull/9132), implementing
[story #8695](https://github.com/aws-e/adp/issues/8695). Other observed overlaps
were already passed code nodes. This is a dated snapshot; newly opened changes
and repair work can introduce further overlap.

- S01 proceeds first; S09 and S12 may then run together in isolated checkouts.
- S09 adds shared-file preflight to its existing `verify-poc.sh` and CI. When a
  POC PR changes `modules/gateway/src/cli_capabilities/contract.py`, it must read
  PR #9132 from GitHub, verify its merged revision belongs to current main and
  its required checks succeeded, then require POC checks on the rebased candidate.
  Open, failed, unavailable, or closed-unmerged receipts cannot pass. A replacement
  dependency needs an explicit reviewed plan revision; never invent success.
- S11 may develop in its own branch while the peer work runs, but cannot merge
  the shared CLI change before that preflight passes. CI/controller observation
  uses bounded retries under the accepted flow limits. Exhaustion is an explicit
  blocked outcome, not a reason to weaken the check.
- Before every POC merge, re-read current main and relevant open PR file lists.
  Reconcile changes to shared Product models, source routes, gateway registration,
  deployment manifests and deploy orchestration. Serialize migration-head edits.
  The source baseline is evidence for authoring, not a branch to restore over
  another flow's work.

No other flow is paused, cancelled or redispatched by this proposal. Its two-action
limit does not reserve global worker, CI or database capacity.

## What the engine can enforce

The current `accepted_node.unresolved_contract_inputs` implementation resolves
`repository:aws-e/adp` as repository availability. It does **not** read an issue or
PR status from `repository:aws-e/adp#NUMBER`. A `node:` input resolves nodes only
inside the same flow. This plan therefore adds no fictional cross-flow node edge.

An `environment_connection:` input must match an admitted connection ID. The
launch materializer replaces every such sentinel with the verified connection.
Target selection, the private ledger and readiness facts are checked from the
bound launch manifest and actual receipts; they are not extra connection IDs.

S04, S13 and S06 retain named capability references to #8374/#8375 using the real
repository scope. Their developers verify the reviewed interface/code handoffs
before merging consumers. S01 checks #8373/#8374/#8375 scope/readiness; S07
rechecks required live behavior before dependent runtime actions. Repository
access, merged code and issue closure cannot substitute for those observations.
These preflights are story implementation obligations, not engine behavior
created by descriptive `required_inputs` entries.

## One coordinated live campaign

S07 uses its existing campaign/verification harness to check current Profile,
adapter and Insights handoffs, compatible installed revisions, runner capacity,
database headroom and the accepted connection before any cloud effect. The
earlier Profile regression and Atlassian release failures require current evidence;
their old statuses do not prove either readiness or a permanent dependency block.

Shared gateway deployments, Terraform states and database migrations use the
canonical component/target locks observed by other writers. S07 must demonstrate
that serialization works across flows, including automatic releases. A POC-only
lock or a file-plan declaration is insufficient. Missing lock coverage blocks the
affected runtime action. Never cancel a peer deployment or roll back its changes.

Record component/source/schema/configuration revisions throughout the 48-hour
soak. Incompatible changes invalidate affected measurements; resume or restart
those checks under the accepted budget and cleanup contract. Default-off flags,
separate connector/collection state and IAM-only cell roles remain required.
