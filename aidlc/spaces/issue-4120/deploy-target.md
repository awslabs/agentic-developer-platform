# Deploy target — intent #4120 / EPIC #4191

**This file is the single source of truth for the deploy literals of this intent.**

Ruling **D-R11** (see [`design-overview.md`](design-overview.md) component-change
map, row 9) amends emission-lint **Rule 2** from a *retype-into-every-issue* form
to a *by-reference* form. Rule 2's intent is preserved in full — credentials are
never ambient, the target is explicit, named, unambiguous, and resolves to
exactly one account. Only the **location** of the literal changed: it lives here,
once.

Issues in this intent cite the target as `deploy-target: adp-dev-embark1` and
**must not** contain an account ID, region, or credential label in their prose.
The motivating incident was a 14-issue configuration hand-patch; retyping these
values into issue bodies is what that ruling exists to prevent.

---

## Resolved literals

| Key | Value |
|---|---|
| Target name | `adp-dev-embark1` |
| AWS account ID | `879318057152` |
| AWS region | `us-east-1` |
| `adp-cred` label | `adp-embark1` |
| Environment | `dev` |
| EKS cluster | `adp-dev-eks-cluster` |
| Gateway namespace | `adp-gateway` |
| Gateway deployment | `bedrockgateway` |
| Terraform var files | `environments/dev/platform.tfvars`, `environments/dev/backend.tfvars` |

### Name-vs-resource gotchas

Three of these are routinely mis-cited; they are called out because each has
already cost an evaluation run:

- **`adp-dev-embark1` is the target *name*, not an AWS resource name.** No EKS
  cluster, namespace, or deployment is called `adp-dev-embark1`. It names the
  account+environment pair; the cluster is `adp-dev-eks-cluster`.
- **`adp-gateway` is the namespace, not the deployment.** The deployment inside
  it is `bedrockgateway`. `kubectl exec -n adp-gateway deploy/adp-gateway` fails;
  `deploy/bedrockgateway` is correct.
- **`adp-dev-cyber-eks` also exists in this account** and is *not* this target.
  Always pass `--name adp-dev-eks-cluster` explicitly.

---

## How to bind credentials

Credentials are **never ambient**. Resolve them one of two ways depending on
where you are running.

**From an agent pod (IRSA / EKS Pod Identity web-identity chain):** this is the
path that auto-refreshes across long operations, which matters because the
injected static session is ~1 hour and Terraform applies routinely outrun it.

```bash
export AWS_ROLE_ARN="arn:aws:iam::879318057152:role/adp-dev-agent-scaledjob-role"
export AWS_WEB_IDENTITY_TOKEN_FILE="/var/run/secrets/eks.amazonaws.com/serviceaccount/token"
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws sts get-caller-identity      # must report the account ID in the table above
```

`adp-cred` does **not** work from the scaledjob pod (returns
`403 unregistered_service_account` for the agent service account). Use the
web-identity chain above.

**From a workstation or any non-pod context:**

```bash
adp-cred assume --service aws --label adp-embark1 --exec <command>
```

**Bind kubectl:**

```bash
aws eks update-kubeconfig --name adp-dev-eks-cluster --region us-east-1
```

---

## Verifying you are on target

Run this before any deploy or evaluation step. It fails loudly rather than
silently acting on the wrong account — the pattern
`.github/workflows/agent-context-verify.yml` already uses.

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
if [ "$ACCOUNT_ID" != "879318057152" ]; then
  echo "FATAL: expected 879318057152 (adp-dev-embark1), got $ACCOUNT_ID"; exit 1
fi
```

---

## Deploy mechanics for this intent

- **Backend/frontend changes** deploy via `gateway-deploy.yml` on merge to the
  default branch.
- **Migrations run automatically.** `gateway-deploy.yml` detects new files under
  `modules/gateway/alembic/versions/**` and chains
  `run-gateway-migrations.yml`, which execs `alembic upgrade head` in the
  running pod. There is **no manual migration step** on a normal merge.
  Consequence worth knowing (issue #4123): migration files are baked into the
  image, so a migration-only push must also rebuild and redeploy the image — the
  workflow already forces this, but do not try to shortcut it.
- **Migration numbering is not stable at authoring time.** Story bodies in this
  intent name migration numbers that were taken by unrelated merges before the
  story ran; wave 1's `026_orchestration_graph` shipped as
  `029_orchestration_graph`. Always resolve the next revision against
  `modules/gateway/alembic/versions/` at implementation time and confirm
  `alembic current` reports a single head. A stale number creates a second head
  and breaks `alembic upgrade head` for everyone.
- **Terraform** for this intent is scoped to `modules/gateway/infra/` (the new
  `modules/orchestration-tick/` module, wave 3+). Waves that add no
  infrastructure require no apply.

---

## References

- [`design-overview.md`](design-overview.md) — component-change map; ruling D-R11 in row 9
- Intent #4120 · EPIC #4191
- `.claude/skills/aidlc-emit-issues/SKILL.md` — emission-lint Rule 2
