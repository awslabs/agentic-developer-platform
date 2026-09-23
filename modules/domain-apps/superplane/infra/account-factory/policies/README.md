# Reviewed IAM artifacts for child-account bootstrap — Issue #5531 (w6-08)

`bootstrap.py` describes three scoped roles the child account needs. Each role is two
documents, and both have to be reviewed:

* a **trust policy** — *who may assume it*, and
* a **permission policy** — *what it may then do*.

Before this directory existed, the plan's commands referenced `bootstrap-trust-policy.json`,
`controller-trust-policy.json` and `workload-trust-policy.json` as bare `file://` arguments,
and none of the three files existed anywhere in the repository. The commands were therefore
not runnable — the review that found this recorded it as "a created account remains
half-built" — and no role had any permission policy at all, so all three would have been
created unusable even if the trust files had appeared.

## Why standalone JSON rather than inline documents

Same reasoning as `platform/infra/securityagent-nightly-iam.tf`: the quality gate parses
these documents and asserts properties about them. Inlining them in Python would leave the
gate asserting a hand-maintained copy while a different document actually applied — the drift
class the gate exists to prevent. `tests/test_bootstrap_policies.py` reads exactly the files
an operator's `file://` argument resolves to.

## Placeholders are deliberately unresolved

Every document carries `${management_account_id}`, and the controller and workload documents
also carry `${child_account_id}`. They are **not** filled in here, because a checked-in
document with a real account id in it is a document that is silently wrong for every other
environment — and the wrongness would be a working role in the wrong organization's account.
Whoever composes bootstrap substitutes them for the accounts actually in play, which is also
the layer that holds credentials. `account_provisioning.bootstrap_runner` takes trust
policies as an **injected string** and refuses a step with no document supplied; it does not
read this directory. That keeps "which document applied" an explicit argument at the call
site rather than a filesystem lookup.

`tests/test_bootstrap_policies.py` asserts the placeholders are still unresolved, so a
convenient hardcoded account id cannot be committed here.

## The two rules every document is held to

Both are inherited from `infra/workspaces/iam.tf`, and both are asserted by parsing the JSON
rather than by grepping for `"*"` — a wildcard in a comment is not a grant:

* **No `Principal: "*"` and no bare-account principal on an assumable role.** A trust policy
  naming only an account id lets *any* principal in that account assume the role, which in a
  workspace account includes every role created there later. Each document names a specific
  role ARN.
* **No `Resource: "*"` in a permission policy**, except where the action takes no resource at
  all (`sts:GetCallerIdentity`, `iam:ListRoles`). Those are stated with a `Sid` that says so.

## The files

| Role | Trust | Permissions | May write IAM |
|------|-------|-------------|---------------|
| `AdpAccountBootstrap` | `bootstrap-trust-policy.json` | `bootstrap-permissions-policy.json` | **yes** — it establishes the other two |
| `AdpWorkspaceController` | `controller-trust-policy.json` | `controller-permissions-policy.json` | no |
| `AdpWorkspaceWorkload` | `workload-trust-policy.json` | `workload-permissions-policy.json` | no |

The tiering is the point, and it is a property of the credential rather than of a review: the
workload role is what arbitrary tenant work runs as, so it must not be able to create an
identity or read the account's baseline controls. One union role would have given it both.

## What these documents do NOT authorize

Nothing here creates a role, attaches a policy, or contacts AWS. They are reviewed artifacts
for a separately-authorized operator or lane to apply. Live child-account bootstrap is a
Wave 6 operations-gate activity and needs its own named authorization.
