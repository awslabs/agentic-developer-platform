# Webhook code profile: admission and first-use procedure

This source is intentionally unbound: Terraform targets default empty and
`webhook-code-manifest.json` has no admitted targets. No live environment,
role, Lambda update, or handler change is included.

The role permits only exact Lambda reads/code updates and a dedicated versioned
S3 prefix. An explicit finite API deny excludes IAM, EKS, role chaining and all
other mutations, including configuration, invocation and alias changes. No
cluster administrator admission is added to the generic deployment profile.

Before binding, the operator must:

1. Inventory exact Lambda function ARNs and execution-role ARNs. Provision each
   execution role's reviewed boundary through its existing infrastructure owner.
   The admission verifier reads the actual role attachment and current boundary
   document, then applies the existing workload ceiling audit. This profile
   admits data-only execution roles: transitive execution or PassRole requires
   a separate reviewed profile. A map entry alone is insufficient.
2. Commit the exact account, region, `lambda-artifacts/...` archive prefix and
   target list to `webhook-code-manifest.json`. Each target has `function_arn`,
   `execution_role`, and the exact packaged `artifact` basename (for example
   `github.zip`). Independently verify these entries equal Terraform's admitted
   function/role map, independent `webhook_code_role_boundaries` inventory and archive prefix. Leave generic `deployment_role_boundaries` unchanged; webhook admission never enables generic EKS access. No runtime target discovery is used.
3. Prepare/review a saved Terraform plan for the dedicated role and policies,
   then apply that exact saved plan. Verify the live retained configuration.
   Immediately re-run operator admission before first dispatch and after any
   role/boundary change; coordinate ownership to prevent concurrent changes.
4. Create protected GitHub environment `adp-webhook-code-dev`: main branch only,
   required reviewer with self-review prevented, admin bypass disabled. Bind only
   its `ADP_WEBHOOK_CODE_ROLE_ARN` variable after admission. Restrict the deployment
   runner group to its reviewed workflow inventory. No generic role fallback.
5. Confirm S3 versioning is enabled. Review the committed source/manifest and
   single bounded deployment intent, then dispatch from main once (attempt1).

The workflow extracts only `git archive` of the exact workflow commit to a fresh
private directory; repository symlinks are refused. Packaging runs from that
clean source before deployment credentials. Untracked and ignored workspace
files cannot enter the archive. Declared dependencies are installed by the
existing reviewed packaging helper. Neither existing bucket objects nor
filename-derived Lambda targets are used to select updates.

The driver first validates every archive and claims a conditional journal at
`<prefix>/receipts/<source-sha>.json`. A second run of the same source is refused.
Before each upload/update it commits intent. Uploads use immutable source/digest
keys; versioning is mandatory. Lambda receives that exact S3ObjectVersion and
RevisionId, with Publish=false. Response/readback must match the local archive's
CodeSha256, expected execution role and returned revision. A conflict, timeout,
ambiguous response, failed journal commit or drift stops all later writes.

The local artifact is an envelope containing `receipt` plus
`commit_acknowledged`. Only an acknowledged final receipt with `complete=true`
and matching durable S3 journal establishes completion. A terminal local intent
alone does not. Retain original evidence on failure; reconcile the exact saved
handle and resource metadata. Never reset/delete journals, replay mutations,
force an update, or restore broad grants automatically. Any continuation needs
its own reviewed source and concrete intent; no force/reset switch is supplied.

Offline validation:

```bash
python3 -m pytest platform/automation-infra/tests/test_webhook_code_profile.py platform/automation-infra/tests/test_webhook_code_driver.py platform/automation-infra/tests/test_workload_inventory.py platform/automation-infra/tests/test_trust_contract.py
terraform -chdir=platform/automation-infra test -filter=tests/webhook_code.tftest.hcl
```

Source merge does not complete S14 runtime rollout. Execution-role readiness,
protected-environment configuration, exact saved plan and one bounded live
acceptance remain operator-owned gates.
