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


## Protected GitHub OIDC admission (#6004)

`orchestrator-trust-policy.json` is the reviewed trust document for
`adp-cli-uplift-eval-orchestrator`. It adds only the GitHub OIDC provider with
`aud=sts.amazonaws.com` and exact subject
`repo:aws-e/adp:environment:dev`. The existing shared-runner/scaledjob AWS
principals are retained for compatibility until their callers are audited;
this artifact does not grant any new service actions or change session duration.

Before updating the live trust, re-read the role and compare its current AWS
principal statements with the retained statements in this file. Stop if another
operator changed them; reconcile the reviewed artifact rather than overwriting
concurrent changes. Verify the `dev` GitHub environment admits only the `main`
branch (no tags), then configure `AWS_CLI_UPLIFT_EVAL_ROLE_ARN` in that environment.
Other environment names require separately reviewed trust and role bindings.

The workflow explicitly clears ambient credentials and uses OIDC without role
chaining. Both evaluation and recovery use the same role selection and reject
non-main refs. Source merge alone does not prove deployment: acceptance requires
live trust readback, successful OIDC exchange with the expected role, and the
existing bounded evaluation/cleanup checks. The executor workflow independently
uses the reviewed-project build dispatcher in `adp-build-dev`; its binding and
existing reviewed executor project must be provisioned before dispatch.


Rollout observation (2026-09-25): the supervising operator applied this additive
trust after a freshness comparison and AWS Access Analyzer validation, and
verified the exact OIDC condition plus unchanged existing AWS principals, service
policies and 10,800-second session limit. The `dev` and `adp-build-dev` GitHub
environments now admit only the `main` branch. At this checkpoint, the eval role
binding and actual OIDC exchange have not been verified, and the executor build
identity/project rollout is still pending. These remaining checks must pass
before claiming live workflow compatibility.
