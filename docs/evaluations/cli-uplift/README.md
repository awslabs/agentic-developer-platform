# CLI-uplift evaluation — IAM artifacts

Least-privilege policy documents applied to the evaluation's own roles, kept here
so a live grant is reviewable in the repo rather than existing only as console
state. These roles are not Terraform-managed (they were created for the
evaluation under issue #5248 and are tagged `Purpose=cli-uplift-eval`).

| File | Role | Policy name | Why |
|------|------|-------------|-----|
| `orchestrator-e02-fixtures-policy.json` | `adp-cli-uplift-eval-orchestrator` | `cli-uplift-eval-e02-fixtures` | E02 provisions its own Cognito challenge + non-admin identities and a run-owned secret; the base policy is read-only for Cognito and could not create or delete them. |

## Scoping notes

- **Cognito is scoped to the single dev user pool ARN.** IAM cannot scope
  `AdminCreateUser`/`AdminDeleteUser` below the pool, so the pool ARN is the
  tightest available resource. Verified: `AdminCreateUser` on any other pool in
  the account is `implicitDeny`.
- **Secrets Manager is scoped to `adp/cli-uplift-eval/adp-e2e-*`** — the run-owned
  fixture prefix only. Verified: `CreateSecret` against the shared login fixture
  (`adp/dev/gateway/test-admin-credentials`) is `implicitDeny`, so a run cannot
  overwrite or delete the fixture the login-regression suite depends on.
- **`DeleteSecret` and `AdminDeleteUser` are included deliberately.** Without
  them the run can create fixtures it cannot remove, which is how an evaluation
  leaks real identities into a shared pool. Cleanup treats a denial as a failure
  rather than a clean teardown, so the absence of these was visible as
  `cleanup: failed` (run 35099197539) rather than silently ignored.

To re-apply:

```bash
aws iam put-role-policy \
  --role-name adp-cli-uplift-eval-orchestrator \
  --policy-name cli-uplift-eval-e02-fixtures \
  --policy-document file://docs/evaluations/cli-uplift/orchestrator-e02-fixtures-policy.json
```
