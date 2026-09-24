# Fixture trusted edge — operator runbook (Issue #5836)

For **root**, to execute under the existing account authorization after PR review.
Target: **879318057152 / dev / us-east-1 / embark1**.

Everything below is the manual equivalent of what a push-triggered workflow would
do. Nothing in this repo runs it automatically, and the component is **default-off**
(`fixture_edge_enabled = false`), so reviewing and merging the PR creates nothing.

> **What this delivers, and what it does not.** This edge is the missing transport
> that lets a protected worker complete a **genuine** bootstrap against an isolated
> fixture gateway — the change #3968's `FIXTURE-ROUTING-CONSTRAINT.md` identified and
> deliberately left unbuilt. The repository tests are mocked (`terraform test`,
> plan-only); they pin configuration properties and **do not** establish live
> acceptance. Steps 5–7 below are what produce live evidence.

---

## 0. What exists, and the one thing this adds

The ordinary edge injects the two trusted headers only on its `AWS_IAM` routes, and
all of them forward to a single ALB that terminates at pods labelled
`app: bedrockgateway`. That is why an isolated fixture is unreachable today, and why
no amount of configuration on the ordinary API fixes it.

This component adds a **second, disposable REST API** whose signed route forwards to
the *fixture's* ALB and injects a *fixture-specific* proof. Consequences worth
knowing before you start:

| | Ordinary edge | Fixture edge (this) |
|---|---|---|
| REST API | `bedrockgw-dev-api` (`59o2rakc50`) | new, per-run, name carries the run nonce |
| Terraform state | ordinary gateway state | **isolated backend key** (step 2) |
| Provenance secret | `/adp/dev/gateway/apigw-provenance-secret` | `/adp/dev/gateway/fixture/<nonce>/apigw-provenance-secret` |
| Lifetime | permanent | deleted with the run |

The two secrets are different values, so a header minted by one edge is **rejected**
by the other gateway. Isolation runs in both directions.

**Prerequisites, and who owns each.** An earlier revision of this runbook said
#3968 creates the fixture's internal ALB. That was **wrong** — its renderer emits a
Deployment, a ClusterIP Service and NetworkPolicies and **no Ingress** — which left a
circular prerequisite where neither side created the ALB this edge forwards to.

| Thing | Owner | How |
|---|---|---|
| fixture Deployment + Service | #3968 | `10-create-fixture.sh` |
| fixture **internal ALB** | **this component** | `scripts/create-fixture-alb.sh` (step 1b) |
| the trusted edge | this component | `scripts/fixture-lifecycle.sh` |
| ledger + teardown of k8s objects | #3968 | `ownership.py` / `90-cleanup-ledger.sh` |

On this EKS Auto Mode cluster one Ingress is one ALB and changing IngressGroup
identity *replaces* the ALB, so a dedicated fixture Ingress is the only way to give
the fixture its own balancer without disturbing the live one. That is a real per-run
cost, and it is why the ALB is created by an explicit step rather than conjured.

---

## 1. Read-only preflight

Nothing here mutates. Confirm the target and collect the four discovered inputs.
Run these with the `adp-embark1` credential.

```bash
# 1a. Confirm the account. Everything keys off this.
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
# EXPECT Account = 879318057152. If it is anything else, STOP.

# 1b. The reused VPC link, and the security groups it may EGRESS to.
#     The link's SG does not have open egress: in dev sg-013f2ce2bcaf1642c permits
#     tcp/80 to three specific groups only. A fixture ALB outside that set would
#     pass every other check and then time out on every request.
aws apigatewayv2 get-vpc-links --region us-east-1 \
  --query 'Items[].{Id:VpcLinkId,Name:Name,Status:VpcLinkStatus,SGs:SecurityGroupIds}' --output table
# EXPECT bedrockgw-dev-vpc-link-v2 AVAILABLE -> vpc_link_id  (dev: qmovr6)

aws ec2 describe-security-groups --group-ids <link-sg-id> --region us-east-1 \
  --query 'SecurityGroups[0].IpPermissionsEgress[].UserIdGroupPairs[].GroupId' --output text
# -> vpc_link_egress_target_security_group_ids (and the group the ALB must reuse)

# 1c. The ORDINARY internal-plane ALB, recorded so the config can refuse to target it.
#     BOTH values are required — an absent one is not a passed check.
aws ssm get-parameter --name /adp/dev/gateway/internal-plane-alb-arn \
  --query Parameter.Value --output text   # -> ordinary_internal_plane_alb_arn
aws ssm get-parameter --name /adp/dev/gateway/internal-plane-alb-dns \
  --query Parameter.Value --output text   # -> ordinary_internal_plane_alb_dns

# 1e. The protected worker role that will call the fixture edge.
aws iam get-role --role-name adp-dev-agent-authority-worker-role \
  --query 'Role.Arn' --output text
# -> allowed_caller_role_arns  (dev: arn:aws:iam::879318057152:role/adp-dev-agent-authority-worker-role)
```

**Why 1c matters.** A "fixture" edge pointed at the ordinary ALB would inject genuine
trusted headers and forward them to the **live** gateway pods, producing a green
result while actually exercising production traffic. The configuration refuses that
combination at plan time, but only if you supply this value.

---

## 1.5 Create the fixture's own internal ALB

**This mutates the cluster.** Run it after #3968's `10-create-fixture.sh` has created
the fixture Deployment and Service, and before the Terraform steps — the edge reads
this ALB, so it must exist first.

```bash
./scripts/create-fixture-alb.sh \
  --run-id "<#3968 run id>" --run-nonce "$FIXTURE_NONCE" \
  --ledger "<#3968 ledger.json>" \
  --namespace adp-gateway --service "<fixture Service name>" \
  --alb-security-groups "<from 1b>"
```

`--check-only` renders and server-side dry-runs it, creating nothing.

It uses `kubectl create` (never `apply`, which would **adopt** a same-named object
and let the ledger authorise deleting something this run did not create), records the
server-assigned uid via #3968's `ownership.py record-k8s`, waits for the ALB, then
verifies on the **live** resource that the scheme really is `internal` and the
`AdpFixtureRun` tag really is this nonce — the tag the Terraform gate refuses to plan
without. It prints `fixture_alb_arn` and `expected_vpc_id` for step 3.

No file under `platform/scripts/operator/wave2/` is modified. An Ingress is an
ordinary uid-bearing Kubernetes object, so #3968's existing `k8s` ledger bucket and
uid-gated delete path already cover its teardown; no new ledger type was needed.

---

## 2. Isolated state, bound to an explicit account and run

Every step below is run through `scripts/fixture-lifecycle.sh`, which refuses to
guess any of the bindings. The prose version of this procedure was not executable —
that is what this script fixes.

```bash
cd modules/gateway/infra/fixture-edge

# The SAME nonce the #3968 ownership ledger generated for this run.
export FIXTURE_NONCE="<nonce from platform/scripts/operator/wave2 lib/ownership.py>"

./scripts/fixture-lifecycle.sh init \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 \
  --region us-east-1 --environment dev \
  --profile adp-embark1 --state-bucket <terraform state bucket>
```

It asserts the live credential really resolves to `879318057152` before doing
anything, binds every AWS call to `--profile`, and uses a state key that carries
**both** the account and the nonce:

```
fixture-edge/dev/879318057152/<nonce>/terraform.tfstate
```

Two concurrent runs cannot corrupt each other, and a mistyped bucket belonging to
another account cannot collide with that account's fixture state.

### What every later command re-checks about that binding

`init` records the resolved backend in `$TF_DATA_DIR/terraform.tfstate` (or
`.terraform/`). Every subcommand that touches state — `plan`, `apply`, `handoff`,
`verify`, `destroy` — re-reads that record and **refuses** unless four things match
its own command line. Checking only the state *key* was not enough:

| Checked | Why the key alone missed it |
|---------|------------------------------|
| `key` | the original check: nonce + account, so `init --nonce A; plan --nonce B` is refused |
| `bucket` | the same key in **another bucket** is a different state file entirely — a typo, or another account's state bucket this credential can reach, passed while reading foreign state |
| `type` | an `s3` record replaced by a `local` one still carries a matching key, and local state has none of the per-run isolation teardown's ownership story rests on |
| `profile` | the backend's profile is the identity that **reads and writes** the state; if it differs from `--profile`, the plan is built from state that the run's account/cluster checks were never made against |

### `--profile` binds the AWS CLI; the provider is bound separately

`terraform` has **no `--profile` flag** — its AWS provider resolves credentials from
the process environment. So a run whose `aws` calls are all correctly bound can still
have `plan`/`apply`/`destroy` act on a *different* account, which is the exact split
the account guards exist to prevent (guards run through the CLI, mutations through
the provider).

Every terraform invocation therefore goes through a wrapper that exports
`AWS_PROFILE=<--profile>` **and clears the higher-precedence variables**
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`,
`AWS_DEFAULT_PROFILE`, `AWS_CREDENTIAL_PROFILES_FILE`). Setting `AWS_PROFILE` alone
is not sufficient: static keys outrank it in the SDK chain, so an operator or CI job
with another account's keys already exported would have terraform use **those** while
`aws --profile` kept reporting the correct account and every guard passed.

### The cluster is pinned by its API endpoint, not its context name

`handoff` and `recover-secret` mutate Kubernetes, and `--profile` does not bind
`kubectl` at all. A kubectl **context name is a local label** chosen by whoever wrote
the kubeconfig: it can read `arn:aws:eks:us-east-1:879318057152:cluster/adp-dev-eks`
while pointing at anyone's API server, so parsing it proves nothing.

The script instead reads the resolved endpoint
(`kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'`) and
compares its host against `aws eks describe-cluster --query cluster.endpoint` for the
named cluster, **through this run's bound profile and region**. That both confirms the
cluster exists in the authorised account and pins the connection kubectl will really
make.

`--expect-cluster` is the **EKS cluster NAME** to verify against AWS — not a string
compared to the context name. (In the previous revision it was the latter, which made
it a free-form bypass: echoing back `kubectl config current-context` always satisfied
it.) Pass it whenever the context is an alias:

```bash
./scripts/fixture-lifecycle.sh handoff ... --expect-cluster adp-dev-eks
```

With an EKS-ARN-shaped context the name is taken from the ARN as a convenience — the
verification against AWS still runs, because the ARN is also just a local string.

> **There is no "just keep state locally" alternative.** An earlier revision of this
> step said you could omit the `-backend-config` flags and keep state locally. That
> is false, and was reproduced as false on Terraform 1.15.3: with a `backend "s3"`
> block declared, omitting them **fails** init —
> `Error: Missing Required Value — The attribute "key" is required by the backend.`
>
> The only two modes are the real run above, and **checks only**:
> `terraform init -backend=false` (supports `fmt`/`validate`/`test`, **not**
> `plan`/`apply`). If truly local state were ever wanted the backend block would have
> to be deleted, not under-configured — and that local file would hold the provenance
> secret in plain text, which is a further reason the secret is not a Terraform output.

---

## 3. Retained inputs, in a private directory

The script keeps run artifacts in `.fixture-run-<nonce>/`, created **700** and
verified to be 700 (a pre-existing loose directory is normalised, not accepted).
Write the reviewed inputs there, so what was applied is auditable:

```bash
ART=".fixture-run-${FIXTURE_NONCE}"
mkdir -p "$ART" && chmod 700 "$ART"

cat > "$ART/fixture.tfvars" <<EOF
fixture_edge_enabled = true

run_nonce           = "${FIXTURE_NONCE}"
expected_account_id = "879318057152"
aws_region          = "us-east-1"
environment         = "dev"

# From step 1b — the script prints these two for you.
fixture_alb_arn = "<create-fixture-alb.sh output>"
expected_vpc_id = "<create-fixture-alb.sh output>"

# From step 1c/1d. Both REQUIRED: a skipped isolation check is not a passed one.
ordinary_internal_plane_alb_arn = "<from 1c>"
ordinary_internal_plane_alb_dns = "<from 1c>"

vpc_link_id                               = "qmovr6"
vpc_link_egress_target_security_group_ids = ["<from 1d>"]

allowed_caller_role_arns = ["arn:aws:iam::879318057152:role/adp-dev-agent-authority-worker-role"]
EOF
```

Note there is **no `fixture_alb_dns` input**: the DNS name is read from the load
balancer named by `fixture_alb_arn`, so the two cannot disagree. This file contains
no secrets — the provenance proof is generated by Terraform and never appears in
inputs, outputs or logs.

---

## 4. Plan, review, then apply the reviewed plan

```bash
./scripts/fixture-lifecycle.sh plan \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1
```

The plan is machine-reviewed before you see it: any change to a resource type
outside this component's expected set **stops the run**. That should be impossible
(separate state, separate API), so it means the wrong backend or directory.

**Expect exactly these**, all name-bound to the nonce:

| Resource | Purpose |
|---|---|
| `terraform_data.run_binding_gate[0]` | blocking isolation preconditions |
| `aws_api_gateway_rest_api.fixture[0]` | the fixture edge |
| `aws_api_gateway_rest_api_policy.fixture[0]` | wrong-role refusal on `/internal` |
| `aws_api_gateway_deployment.fixture[0]` | |
| `aws_api_gateway_stage.fixture[0]` | stage `dev` |
| `aws_cloudwatch_log_group.fixture[0]` | 7-day access logs |
| `aws_ssm_parameter.fixture_provenance_secret[0]` | SecureString handoff |
| `random_password.fixture_edge_provenance[0]` | (no cloud resource) |

A plan-time **refusal** here is the gate working. `terraform_data.run_binding_gate`
fails the plan — exit code 1, not a warning — if the caller's real account or region
disagrees, or if the fixture ALB is public, in another VPC, not tagged for this run,
unreachable from the reused VPC Link, or is the ordinary internal-plane ALB.

```bash
./scripts/fixture-lifecycle.sh apply \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1 \
  --plan-file ".fixture-run-${FIXTURE_NONCE}/fixture.plan"
```

`apply` takes a **reviewed plan file only**; it will not generate a fresh plan. It
writes an ownership receipt to the artifact directory afterwards.

---

## 5. Hand the fixture gateway its secret — and actually attach it

```bash
./scripts/fixture-lifecycle.sh handoff \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1 \
  --ledger <#3968 ledger.json> \
  --namespace adp-gateway \
  --fixture-deployment w2-fixture-gateway-<run-id>
```

**Why `--fixture-deployment` is mandatory.** An earlier revision created the Secret
and stopped. That changed nothing: the fixture pod kept reading the **ordinary**
gateway's `bedrockgateway-secrets/apigw-provenance-secret`, so it validated against
production's value. The Secret alone is inert. This step:

1. reads the per-run SSM parameter and **refuses** any path that is not
   `.../fixture/<nonce>/...`, so the two edges cannot share one trust root;
2. pipes the value SSM → `kubectl` stdin — never a file, variable, log or argv — and
   checks the **SSM exit status separately** from `kubectl`'s, because a Secret built
   from a failed read would hold an error string and surface much later as an
   unexplained 403;
3. refuses to adopt an existing Secret, and records the server-assigned uid in the
   #3968 ledger, failing loudly (with the manual `kubectl delete` to run) if that
   record cannot be written;
4. **attaches** it with a strategic-merge patch that repoints
   `BG_APIGW_PROVENANCE_SECRET` and sets `BG_TRUST_APIGW_HEADERS=true` on the fixture
   Deployment, and then **verifies** all nine secret-backed env refs survived.
   `container.env` merges by name, so the eight refs #3968's renderer deliberately
   carried over are preserved; a json-merge patch would replace the whole list and
   silently drop them.

`BG_TRUST_APIGW_HEADERS=true` is defensible on the fixture **only because this
component blanks both trusted headers on its auth-NONE route**. Never set it on the
ordinary gateway.

The pod must roll for any of this to take effect:

```bash
kubectl rollout status deployment/w2-fixture-gateway-<run-id> -n adp-gateway
```

---

## 6. Verify the endpoint's identity — before pointing a worker at it

This is the step that proves isolation rather than assuming it.

```bash
ENDPOINT="$(terraform output -raw worker_control_endpoint)"
API_ID="$(terraform output -raw rest_api_id)"
echo "$ENDPOINT"
# EXPECT https://<API_ID>.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent
```

**6a. It must NOT be the ordinary API.**

```bash
test "$API_ID" != "59o2rakc50" && echo "OK: distinct from the ordinary edge" || echo "STOP"
```

**6b. It must resolve to the fixture pods, not the ordinary ones.** Confirm the
fixture ALB's target group holds the fixture pod IPs:

```bash
aws elbv2 describe-target-health \
  --target-group-arn "$(aws elbv2 describe-target-groups \
      --load-balancer-arn '<fixture_alb_arn>' \
      --query 'TargetGroups[0].TargetGroupArn' --output text)" \
  --query 'TargetHealthDescriptions[].{Target:Target.Id,State:TargetHealth.State}' --output table
# Cross-check against: kubectl get pods -n adp-gateway -l <fixture selector> -o wide
```

**6c. The refusals — and a positive control.** Each must be observed, not assumed,
and each must be attributed to **the layer that actually refused it**. A 403 proves
nothing if you cannot say which component produced it; three different mechanisms are
involved here and only one of them is this component's resource policy.

| # | Request | Expected | **Which layer refuses, and why** |
|---|---|---|---|
| 1 | unsigned, on `/internal` | 403 | **API Gateway `AWS_IAM`** — no valid SigV4, rejected before any policy or pod is consulted |
| 2 | unsigned + forged trusted headers | 403 | **Same as #1.** The forged headers are irrelevant: the request never reaches the pod, and the edge would overwrite both anyway |
| 3 | correctly signed, role NOT allow-listed | 403 | **This component's resource policy** — the explicit `Deny` on `/internal`, matched by `aws:PrincipalArn` |
| 4 | forged provenance header that *did* reach the pod | 403 | **The gateway pod** — `src/auth/caller_provenance.py` constant-time compare against the per-run secret |
| 5 | **human bearer session on a non-`/internal` route** | **NOT 403** | **Positive control.** Must succeed; see below |

```bash
# 1. UNSIGNED -> refused by API Gateway's AWS_IAM, before the policy or the pod.
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$ENDPOINT/bootstrap"

# 2. SPOOFED headers, unsigned -> same layer as #1; the edge also OVERWRITES both.
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$ENDPOINT/bootstrap" \
  -H 'X-Caller-Identity: arn:aws:iam::879318057152:role/anything' \
  -H 'X-Adp-Edge-Provenance: forged'

# 3. WRONG ROLE, correctly signed -> refused by THIS component's resource policy.
#    Use a signed identity NOT in allowed_caller_role_arns. Do NOT use the worker role.
awscurl --service execute-api --region us-east-1 -X POST "$ENDPOINT/bootstrap" -d '{}'
```

Confirm #3 was the *policy* and not something else, by reading the access log rather
than inferring it from the status code:

```bash
aws logs tail "/aws/api-gateway/bedrockgw-dev-w2fx-${FIXTURE_NONCE}" --since 5m
```

**5 — the positive human control, and why it must be run.** The previous revision's
resource policy denied everything under `execution_arn/*`, including the auth-`NONE`
human routes. An unsigned human request carries no `aws:PrincipalArn`, so it matched
the `Deny` and was refused **at the edge, before the gateway could authenticate the
JWT at all**. The policy is now scoped to `/internal` only, and this control is what
proves it: a human bearer-token request on a non-`/internal` route must be handled by
the gateway (200/401/403 *from the application*), **not** refused by the edge.

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://${API_ID}.execute-api.us-east-1.amazonaws.com/dev/api/health"
# EXPECT a normal application response, NOT an edge refusal. If this is refused by
# API Gateway, the Deny has regressed to API-wide and human sign-in is broken.
```

Expected: **403 for 1–4, and a normal application response for 5.** Capture each
status code plus its attributed layer as evidence. A `200` on 1–4, or an edge refusal
on 5, means STOP. A `5xx` on 1 or 2 also means STOP: it can indicate the request
reached a backend, which those two must never do.

---

## 7. Point the protected worker at it

Set on the **fixture worker Job only**:

```
ADP_AGENT_CONTROL_ENDPOINT = <worker_control_endpoint, from step 4's outputs>
```

The worker appends `/bootstrap` itself and rejects any endpoint that is not HTTPS
with a bare host, which is why the published value is an HTTPS execute-api URL
already ending at `/internal/v1/agent`.

The fixture worker pod must still satisfy the gateway's workload verifier, and those
constraints are **read from the verifying gateway's own environment** — so the
fixture gateway's `AGENT_WORKER_NAMESPACE` / `AGENT_WORKER_SERVICE_ACCOUNT` must
match where the Job actually runs, and the image must be digest-pinned to an
already-approved digest. The Job must run its real entrypoint (no `command`/`args`)
and carry exactly `ADP_AGENT_AUTHORITY_ENABLED=true`. Those requirements are owned by
the #3968 fixture tooling; this component only provides the transport.

A successful bootstrap here is the live acceptance evidence for #3968 — **with no
fabricated headers anywhere in the path.**

---

## 8. Teardown, in dependency order, with proof

**Ordering is load-bearing, and it is the opposite of the intuitive one.** `main.tf`
reads the fixture ALB (`data.aws_lb.fixture`) to derive its DNS name, and Terraform
re-reads data sources during `destroy`. So deleting the fixture Ingress first makes
the destroy **unplannable** — reproduced on Terraform 1.15.3:

```
Error: ... data source error — the object cannot be read
```

Therefore: **destroy the edge first** (which stops the listeners and routes), **then**
delete the Ingress and its ALB.

```bash
# Review first — this deletes nothing.
./scripts/fixture-lifecycle.sh destroy \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1 --dry-run

# Then for real.
./scripts/fixture-lifecycle.sh destroy \
  --nonce "$FIXTURE_NONCE" --account-id 879318057152 --profile adp-embark1
```

What it does, and what it refuses:

1. **Plans the destroy from exact state** and prints the precise list of objects
   state owns. A plan that would also *create* or *update* anything stops the run.
2. **Refuses to run without the reviewed `fixture.tfvars`.** It will not fall back to
   deleting by name prefix or tag: a name prefix is not ownership, and a same-named
   replacement created by someone else would be destroyed instead. This is why
   replacement-safe teardown needs exact state, not a post-hoc sweep.
3. **Destroys the edge**, then **verifies absence** rather than trusting the summary —
   the REST API and the per-run secret must be gone, and the **ordinary** provenance
   parameter must still be present. Any unverified absence fails the command, so
   cleanup is never reported as complete on an unproven teardown.

If the ALB was already deleted out of order, the destroy plan cannot be read. Use:

```bash
./scripts/fixture-lifecycle.sh destroy ... --recover   # adds -refresh=false
```

That path was verified to complete. It is a recovery, not the normal route.

**Then, and only then, the Kubernetes objects:**

```bash
# #3968's ledger deletes the fixture Ingress (this is what removes the ALB) and the
# fixture Secret, both uid-gated.
platform/scripts/operator/wave2/90-cleanup-ledger.sh --ledger <ledger.json>
```

Finally remove the private artifact directory `.fixture-run-<nonce>/`, which holds the
reviewed inputs and plan files.

**Ownership model, and its honest boundary.** The fixture Ingress and Secret are in
#3968's ledger with their server-assigned uids, so its cleanup deletes them only when
the live object is still the one this run created. The **Terraform-owned** resources
(REST API, policy, deployment, stage, log group, SSM parameter) are owned by the
isolated per-run **state file**, which is a stronger record than either a name prefix
or a ledger row — and they are removed by the reviewed destroy plan above. The
`ownership` output and the receipt in the artifact directory exist so a human reading
the ledger can see these resources and how they are torn down; they are *not* the
delete authority. #3968's `cleanup_ok` stays `None` for a dry run and `False` on any
unverified absence, so do not record success for this component until both the destroy
verification and `90-cleanup-ledger.sh` report verified absence.

## What CI checks before any of this runs

`.github/workflows/fixture-edge-ci.yml` runs on every PR touching this directory:
`terraform fmt -check`, `validate`, the 25 mocked `terraform test` run blocks, the
three pytest suites, and `bash -n` on every script.

It holds **no AWS credentials**: no `id-token: write`, no credential-configuration
step, unusable `AWS_*` values with IMDS disabled, and every `terraform init` uses
`-backend=false`. That last one is structural rather than stylistic — with the
`backend "s3"` block declared, a `plan` after a `-backend=false` init exits non-zero
with *Backend initialization required*, so CI cannot plan, apply or write state even
by accident.

Two things worth knowing if you are reading a green check on a PR here:

- Before this workflow existed, a fixture-edge-only PR triggered **`Gateway Infra
  Plan`** (it globs `modules/gateway/infra/**`), which authenticates to the account
  and plans the *ordinary* gateway. This root is not a module of that one, so that
  job never evaluated a line of this component while still reporting green. If you
  see only that check on a fixture-edge PR, this component was not tested.
- A green **`Fixture Edge CI`** proves configuration properties: default-off, the
  run-binding gate refusing mismatched discovered inputs, the Deny staying scoped to
  `/internal`, no output carrying the provenance secret, and the scripts refusing
  the unsafe orderings. It proves **nothing about live behaviour**. Steps 6 and 7
  are the only source of that.

`tests/test_ci_wiring.py` gates the workflow itself — that the trigger paths reach
this component and #3968's `lib/ownership.py`, that every suite in `tests/` is
actually invoked, and that no step can reach AWS.

## Boundaries

- **No ordinary rollout, flag change or platform apply** is part of this procedure.
- **No IAM change is required.** The worker role's existing grants already cover
  `/internal/*` with a wildcard API id, so supported access needed no widening;
  restriction lives on the fixture API's resource policy instead.
- The provenance secret must never be echoed, committed, or pasted into an issue
  comment or workflow log. Step 5 pipes it directly.
- The repository tests are mocked and prove configuration properties only. Live
  acceptance comes from steps 6 and 7.
