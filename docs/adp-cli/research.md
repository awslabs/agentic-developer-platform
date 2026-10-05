# Superplane research

Research uses the selected ADP deployment and human login through the existing Superplane gateway. Findings and proposals are scoped by the domain's verified organization. Reads never start a scan.

```sh
adp superplane research findings list --page-size 20 --max-pages 5 --json
adp superplane research findings show FINDING_UUID --json
adp superplane research sources --json
adp superplane research stats --json
adp superplane research proposal list --status proposed --workspace WORKSPACE_UUID --json
adp superplane research proposal show PROPOSAL_UUID --json
adp superplane research proposal create --request-file proposal.json --request-id UUID --dry-run --json
adp superplane research proposal create --request-file proposal.json --request-id UUID --yes --json
adp superplane research proposal approve PROPOSAL_UUID --expect-revision HASH --yes --json
adp superplane research proposal reject PROPOSAL_UUID --expect-revision HASH --reason 'Evidence incomplete' --yes --json
```

Optional `--start`/`--end` narrow findings by scan time and proposals by creation time; both must be timezone-aware, increasing, and within 90 days. Start is inclusive and end exclusive. Lists use the existing offset pagination, bounded to 100 records per page and 100 pages per command. Continue with `next_page` and the same filters; these reads are not frozen snapshots. A duplicate encountered across pages fails rather than hiding it. Stable secondary ID ordering resolves timestamp ties. Empty results are not proof a scan ran. Sources expose static scanner descriptions; detail retains existing authorized provenance and evidence content.

Proposal input follows the existing domain schema: workspace_id, title, objective, hypothesis, optional source_findings, cost/duration estimates, required_resources and experiment_plan. A request UUID is bound to a tenant-derived proposal UUID. Same input replays the current proposal; changed input conflicts. Source findings must belong to the target workspace and organization. Cost/duration values on idempotent creates must fit two decimal places; this is the existing database precision, not proof of a settled charge. On uncertain transport, retain the original request ID and inspect proposals; the CLI never automatically repeats a mutation.

Show returns a revision hash over proposal content, current state and timestamp. Approval/rejection requires that hash and a verified human principal, checked while locking the proposal row. An intervening change yields 409. Approval changes the existing proposal to approved and makes it eligible for the domain experiment queue; it is not an implementation, merge or deployment approval and does not prove work completed. The new support endpoint must advertise the revision/idempotency contract before CLI writes; older domains fail without posting a mutation.

`scan` and `proposal generate` report unavailable without sending work: the current synchronous backend has no durable bounded research request/reservation contract. Reusing provider-bootstrap lifecycle records would introduce unrelated execution authority. Their paid recovery acceptance remains open until a domain-owned supported job contract exists.

E25 runs these reads on the existing disposable EC2 regression client using the served installed CLI and isolated session stores. Select `--suite research` or the existing nightly suite, with `research_readback: true` in the run configuration (or existing `CLI_UPLIFT_EVAL_BINDINGS` non-secret overlay) for an existing domain on this gateway. The separate read-only fixture gate blocks E25 when this is absent; it does not claim service readiness or borrow E18 mutation recovery. Actual authenticated CLI reads must succeed. It requires the existing Superplane domain and performs no paid scan or decision. Source tests and E25 are not the complete #5639 acceptance: bounded scan/generation, interrupted concurrent decision acceptance and verified live proposal cleanup remain open.

The canonical helper is `modules/domain-apps/superplane/cli/adp-superplane-research.py`. The byte-identical gateway CLI artifact is packaged for the existing gateway-only Docker build context; a regression prevents drift.
