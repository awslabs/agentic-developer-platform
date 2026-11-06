# Atlassian foundation activation handoff

## What is enabled

The deployment manifest has separate foundation targets for the gateway backend
and standalone adapter. Both belong to the `atlassian-foundation` release group.
The existing ungrouped gateway target is unchanged: ordinary gateway deployment
does not acquire foundation authority or select the adapter stage.

The operator reported successful installed-resource and registered-connection
readbacks before activation. Those checks cover the dedicated namespace, build
and verification prerequisites, scoped mutation roles, read-only verifier access,
and adapter pod role. Protected adapter settings and the gateway guard settings
were also read back. These are prerequisite checks, not evidence that the adapter
has been deployed or that native runtime acceptance has passed. Exact target
identities and readback records remain in the operator's private evidence store.

## How release authority is retained

The manifest pins a reviewed baseline. The controller may select a later reviewed,
merged source and workflow only through the existing accepted release ledger.
Reservation, signed-job admission and final observation must agree with that
ledger and the exact target; a branch name or workflow input alone grants nothing.

Both foundation baselines now name the reviewed merge of #8577,
`166dfa700e2b10fcfa419ffafa6a1461d8c519a7`. The earlier PR-branch baseline was
not an ancestor of this squash merge, so release admission correctly refused it.
After a squash merge, activation must pin the actual merge commit on the default
branch. Successful builds or matching infrastructure files do not establish Git
ancestry. Updating these pins retains the existing ancestry and release checks.

The gateway workflow's `release_group` input defaults to `none`. Only an accepted
foundation reservation selects `atlassian-foundation`. Before obtaining mutation
credentials, the backend job uses GitHub's signed job identity to check the current
claim, release and shared lease with the gateway. The verifier records the exact
run and attempt for final receipt validation. The guard is loaded from the reviewed
workflow revision, not supplied by deployment input. Missing configuration, a
rerun, a different role or refused authority stops the guarded job.

The foundation path does not run the frontend deployment. Ordinary manual,
automatic and ungrouped engine releases retain their existing path. Workflow
context records the selected group, so final observation cannot silently accept
an ordinary run in place of the guarded foundation run.

## Protected configuration and remaining operator work

The gateway's protected deployment environment supplies:

- `ADP_FOUNDATION_GATEWAY_URL`: the registered gateway endpoint.
- `ADP_FOUNDATION_CONNECTION_ROLE_ARN`: the verified read-only connection role.
- `ADP_DEPLOY_ROLE_ARN`: the separately scoped gateway mutation role.

Never put those deployment values, credentials or private readback documents in
this public handoff. The operator has confirmed these settings; they are not an
outstanding approval request.

The root operator must bind the reviewed merge of this baseline repair as a
predecessor of both foundation operations, so a later core stage cannot restore
the superseded manifest. Preserve the other predecessors and evaluation checks.
After the reviewed merge, the root operator owns setting the adapter's expected
gateway revision, amending the accepted evaluation to the final source/workflow
revisions, and executing the protected foundation stages. Runtime readback,
controlled acceptance, cleanup and recovery handoff remain pending until those
stages produce verified native receipts. The shared group and backend generation
settings must match the accepted release; they are not inferred from a successful
build. Coordinated cutover remains with #8453 and final live acceptance with #8454.

## Local verification

From `modules/gateway`, using the repository's Python 3.12 development environment:

```bash
python -m pytest -q tests/orchestration/test_foundation_release_guard.py \
  tests/agentauth/test_actions_routes.py \
  tests/orchestration/test_deployment_manifest.py \
  tests/orchestration/test_deployment_workflow_contract.py \
  tests/orchestration/test_operations_evidence.py \
  tests/orchestration/test_operations_release_job.py
```

These tests exercise controlled authorization, input binding and receipt behavior.
They do not contact the deployment target or establish live provider compatibility.
