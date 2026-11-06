# Operational tools-platform release smoke

This opt-in policy supports explicitly authorized dev maintenance when native
all-in financial qualification is not requested. It runs within the existing
protected tools deployment workflow and keeps its IAM, immutable source,
interceptor, exact saved-plan, target readiness and private evidence gates.
Production and engine-correlated releases cannot select this policy. There is
no automatic fallback when native admission fails.

The operator sets `ADP_TOOLS_RELEASE_QUALIFICATION=operational` on the protected
`adp-tools-deploy-dev` environment. Put a current dedicated ordinary-user Cognito
access JWT in its `ADP_TOOLS_SMOKE_OPERATIONAL_ACCESS_TOKEN` secret. Do not use
an administrator session or a designated native identity. Existing consent,
allow lists, entitlements, admission and execution ownership remain enforced
through the ordinary Gateway APIs; this policy grants none of them.

Set the protected `ADP_TOOLS_OPERATIONAL_SMOKE_JSON` variable to an object with
exactly these fields:

- `version`: integer `1`.
- `scope`: `ordinary_user_behavior_only`.
- `source_revision` and `definition_revision`: the exact reviewed 40-hex commits
  selected for the source and actual workflow dispatch.
- `gateway_origin`: the HTTPS public ADP origin, matching the existing protected
  `ADP_TOOLS_EVAL_GATEWAY_URL`, without a path.
- `subject_id`: the fixture's real Cognito subject; `user_id`: its canonical ADP
  user ID; `tenant_id` and `team_id`: its current membership.
- `query`: an explicitly selected nonblank search query, at most 200 characters.
- `authorized_at` and `expires_at`: integer Unix timestamps for the actual
  approved release window, at most one hour. They are not native approval clocks.

Dispatch the existing workflow on main, attempt 1, using a manual reviewed source
and no engine correlation. The runner pins the operational validator and release
driver to the actual workflow revision. Before assuming the deployment role it
checks the configured access JWT against live `/auth/me`, requiring the exact
non-admin fixture. Expired or mismatched inputs fail before Terraform apply.
The normal 12-minute outer job bound remains in place.

After matching READY readback, the smoke checks the same identity and canonical
owner, performs live discovery, submits one exact web search through AgentCore,
closes the original owned execution, reads back its confirmed result and repeats
discovery and identity checks. An unconfirmed search is not resent; one recovery
of its original request can locate it for cleanup. A changed catalogue, foreign
receipt, revoked identity, unsuccessful search or unconfirmed cleanup refuses
release publication. Requests and responses remain size/time bounded. Metadata
bootstrap and this search smoke do not establish live Knowledge acceptance;
complete the separate authenticated Door acceptance after publishing its actual
READY target ID.

Receipts contain `operational_smoke`, `scope: ordinary_user_behavior_only`,
`cost_qualification: not_requested`, and `governance_complete: false`. No quoted
maximum AWS cost, synthetic native approval, money reservation or settled cost is
created. Actual charges remain subject to ordinary accounting. This policy does
not promise that total AWS charges stay below USD20.

The existing private publisher retains only bounded delivery/smoke receipts,
without JWT, query or provider bodies. Final release qualification is
`operational_smoke_verified_evidence_retained` only after immutable evidence
retention succeeds. Failure leaves no successful final release artifact. Input
JWT and configuration files are removed in the workflow's always-run cleanup.

After acceptance, revoke/delete the disposable fixture and grants, delete its
protected token and configuration, and restore the qualification variable to
`native` (or remove it). Preserve existing users, targets and audit history. The
native policy, its original approval clocks and all-in funding guarantees remain
unchanged whenever that policy is selected.
