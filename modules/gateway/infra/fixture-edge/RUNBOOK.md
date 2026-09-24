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

**Prerequisite owned by the #3968 fixture tooling, not by this component:** the
fixture gateway Deployment/Service and its **own internal ALB** (via a fixture
Ingress) must already exist. On this EKS Auto Mode cluster a second Ingress cannot
join an existing ALB, so the fixture ALB is necessarily a new one — that is a real
per-run cost and the reason this component takes the ALB as an input rather than
creating it.

---

## 1. Read-only preflight

Nothing here mutates. Confirm the target and collect the four discovered inputs.
Run these with the `adp-embark1` credential.

```bash
# 1a. Confirm the account. Everything keys off this.
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output table
# EXPECT Account = 879318057152. If it is anything else, STOP.

# 1b. The fixture's OWN internal ALB (created by the #3968 fixture tooling).
#     Replace the name filter with the fixture Ingress's ALB.
aws elbv2 describe-load-balancers --region us-east-1 \
  --query 'LoadBalancers[?contains(LoadBalancerName, `w2-fixture`)].{Name:LoadBalancerName,Arn:LoadBalancerArn,DNS:DNSName,Scheme:Scheme}' \
  --output table
# EXPECT exactly one, Scheme = internal.
# Take LoadBalancerArn -> fixture_alb_arn  (a LOAD BALANCER arn, never a listener arn)
# Take DNSName         -> fixture_alb_dns

# 1c. The ORDINARY internal-plane ALB, recorded so the config can refuse to target it.
aws elbv2 describe-load-balancers --region us-east-1 \
  --query 'LoadBalancers[?contains(LoadBalancerName, `bedrockg`)].{Name:LoadBalancerName,Arn:LoadBalancerArn}' \
  --output table
# Take the internal-plane one -> ordinary_internal_plane_alb_arn

# 1d. The existing VPC link, reused rather than duplicated.
aws apigatewayv2 get-vpc-links --region us-east-1 \
  --query 'Items[].{Id:VpcLinkId,Name:Name,Status:VpcLinkStatus}' --output table
# EXPECT bedrockgw-dev-vpc-link-v2 AVAILABLE -> vpc_link_id  (dev: qmovr6)

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

## 2. Isolated state, and the run nonce

Use the **#3968 run nonce** so the edge is bound to the same run as the rest of the
fixture, and an isolated backend key so this can never touch ordinary gateway state.

```bash
cd modules/gateway/infra/fixture-edge

# Use the SAME nonce the #3968 ownership ledger generated for this run.
export FIXTURE_NONCE="<nonce from platform/scripts/operator/wave2 lib/ownership.py>"

terraform init -input=false \
  -backend-config="bucket=<terraform state bucket>" \
  -backend-config="key=fixture-edge/dev/${FIXTURE_NONCE}/terraform.tfstate" \
  -backend-config="region=us-east-1" \
  -backend-config="encrypt=true"
```

A per-nonce key means two concurrent fixture runs cannot corrupt each other, and
destroying one run's edge cannot affect another's.

> Local-only alternative if you prefer no remote state for a disposable resource:
> omit the `-backend-config` flags and keep `terraform.tfstate` locally. **Do not
> commit it** — it contains the provenance secret in plain text. This is exactly why
> the secret is not a Terraform output.

---

## 3. Retained inputs

Write the reviewed inputs to a file rather than passing them ad hoc, so what was
applied is auditable. Values come from step 1.

```bash
cat > "fixture-${FIXTURE_NONCE}.tfvars" <<EOF
fixture_edge_enabled = true

run_nonce           = "${FIXTURE_NONCE}"
expected_account_id = "879318057152"
aws_region          = "us-east-1"
environment         = "dev"

fixture_alb_arn = "<from 1b LoadBalancerArn>"
fixture_alb_dns = "<from 1b DNSName>"

ordinary_internal_plane_alb_arn = "<from 1c>"

vpc_link_id = "qmovr6"

allowed_caller_role_arns = ["arn:aws:iam::879318057152:role/adp-dev-agent-authority-worker-role"]
EOF
```

This file contains no secrets. The provenance proof is generated by Terraform and
never appears in inputs, outputs or logs.

---

## 4. Plan, then apply

```bash
terraform plan -input=false -var-file="fixture-${FIXTURE_NONCE}.tfvars" -out=fixture.plan
```

**Review the plan for exactly these 6 resources**, all name-bound to the nonce:

| Resource | Purpose |
|---|---|
| `aws_api_gateway_rest_api.fixture[0]` | the fixture edge |
| `aws_api_gateway_rest_api_policy.fixture[0]` | wrong-role refusal |
| `aws_api_gateway_deployment.fixture[0]` | |
| `aws_api_gateway_stage.fixture[0]` | stage `dev` |
| `aws_cloudwatch_log_group.fixture[0]` | 7-day access logs |
| `aws_ssm_parameter.fixture_provenance_secret[0]` | SecureString handoff |
| `random_password.fixture_edge_provenance[0]` | (no cloud resource) |

If the plan shows **any** resource outside this directory, or any change to
`bedrockgw-dev-api`, the ordinary ALBs, IAM, or the ordinary provenance parameter —
**STOP**. It should be impossible (separate state, separate API), and a plan that
shows otherwise means the wrong backend or directory.

A plan-time refusal here is the config working as intended. `run_binding` fails if
the caller's real account is not 879318057152, the region disagrees, or
`fixture_alb_arn` is the ordinary internal-plane ALB.

```bash
terraform apply -input=false fixture.plan
```

Record the outputs. `worker_control_endpoint` and `ssm_provenance_parameter_name` are
the two the fixture tooling needs; **`terraform output` never prints the secret.**

---

## 5. Hand the fixture gateway its matching secret

The fixture pod must validate against the **same** value this edge injects, or every
internal call returns 403. Fetch it directly into the fixture's own Secret — never
via an intermediate file, an issue comment, or a workflow log.

```bash
# Read the per-run secret (decrypted) and place it in the FIXTURE's secret only.
aws ssm get-parameter \
  --name "$(terraform output -raw ssm_provenance_parameter_name)" \
  --with-decryption --query 'Parameter.Value' --output text \
| kubectl create secret generic "w2-fixture-provenance-${FIXTURE_NONCE}" \
    --namespace adp-gateway \
    --from-file=BG_APIGW_PROVENANCE_SECRET=/dev/stdin
```

The fixture gateway also needs `BG_TRUST_APIGW_HEADERS: "true"`, which is the claim
that a header-blanking edge fronts it. That is true for the fixture **only because
this component blanks both headers on its auth-NONE route** — so set it on the
fixture's own ConfigMap, and nowhere else.

> Add this Secret to the #3968 cleanup ledger's `k8s` rows so teardown removes it.
> Do **not** set either value on the ordinary gateway's ConfigMap or Secret.

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

**6c. The three refusals.** Each must be observed, not assumed. These are the
acceptance evidence for "spoofed, unsigned and wrong-role calls refused".

```bash
# UNSIGNED -> 403 from API Gateway ("Missing Authentication Token"/"not authorized").
# It never reaches the pod, so it cannot acquire a provenance header.
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$ENDPOINT/bootstrap"

# SPOOFED headers, unsigned -> still 403 at the edge. The edge OVERWRITES both
# headers, so a client-supplied pair cannot survive even if signing succeeded.
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$ENDPOINT/bootstrap" \
  -H 'X-Caller-Identity: arn:aws:iam::879318057152:role/anything' \
  -H 'X-Adp-Edge-Provenance: forged'

# WRONG ROLE, correctly signed -> 403 from the resource policy's explicit Deny.
# Use any signed identity NOT in allowed_caller_role_arns (e.g. your own session).
# awscurl or a SigV4 signer; do NOT use the worker role here.
awscurl --service execute-api --region us-east-1 -X POST "$ENDPOINT/bootstrap" -d '{}'
```

Expected: **403 for all three.** Capture each status code as evidence. A `200` or a
`5xx` on any of them means STOP and investigate — a 5xx can indicate the request
reached a backend, which the first two must never do.

---

## 7. Point the protected worker at it

Set on the **fixture worker Job only**:

```
ADP_AGENT_CONTROL_ENDPOINT = <worker_control_endpoint from step 4>
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

## 8. Teardown, with proof

Exact and observable, per #5836.

```bash
cd modules/gateway/infra/fixture-edge
terraform destroy -input=false -var-file="fixture-${FIXTURE_NONCE}.tfvars"
```

Then **prove** removal rather than trusting the destroy summary:

```bash
# 8a. The REST API is gone.
aws apigateway get-rest-api --rest-api-id "$API_ID" 2>&1 | grep -q 'NotFoundException' \
  && echo "OK: fixture API deleted" || echo "STILL PRESENT"

# 8b. No fixture edge survives under this nonce (catches a partial destroy).
aws apigateway get-rest-apis --region us-east-1 \
  --query "items[?contains(name, '${FIXTURE_NONCE}')].{id:id,name:name}" --output table
# EXPECT empty.

# 8c. The per-run secret is gone.
aws ssm get-parameter --name "/adp/dev/gateway/fixture/${FIXTURE_NONCE}/apigw-provenance-secret" 2>&1 \
  | grep -q 'ParameterNotFound' && echo "OK: secret deleted" || echo "STILL PRESENT"

# 8d. The log group is gone.
aws logs describe-log-groups \
  --log-group-name-prefix "/aws/api-gateway/bedrockgw-dev-w2fx-${FIXTURE_NONCE}" \
  --query 'logGroups[].logGroupName' --output table
# EXPECT empty.

# 8e. The ORDINARY edge is untouched — the most important post-check.
aws apigateway get-rest-api --rest-api-id 59o2rakc50 --query 'name' --output text
# EXPECT bedrockgw-dev-api
aws ssm get-parameter --name /adp/dev/gateway/apigw-provenance-secret \
  --query 'Parameter.Name' --output text
# EXPECT the parameter still present (value not printed).
```

Also delete the fixture Secret from step 5 (via the #3968 ledger), and the
`fixture-<nonce>.tfvars` and any local state file.

**Ownership note.** The `ownership` Terraform output enumerates all three deletable
resources with a re-verification command each, for the #3968 ledger. Teardown there
deletes only when the server-side identity still matches what creation recorded — so
a same-named replacement created by someone else is left alone rather than destroyed.
A name prefix alone is not ownership.

### If destroy fails partway

State is isolated per nonce, so re-running `terraform destroy` is safe and
idempotent. If state is lost, delete by nonce using 8b/8c/8d to locate resources,
verifying the `AdpFixtureRun` tag matches your nonce before each delete:

```bash
aws apigateway get-rest-api --rest-api-id <id> --query 'tags.AdpFixtureRun' --output text
```

---

## Boundaries

- **No ordinary rollout, flag change or platform apply** is part of this procedure.
- **No IAM change is required.** The worker role's existing grants already cover
  `/internal/*` with a wildcard API id, so supported access needed no widening;
  restriction lives on the fixture API's resource policy instead.
- The provenance secret must never be echoed, committed, or pasted into an issue
  comment or workflow log. Step 5 pipes it directly.
- The repository tests are mocked and prove configuration properties only. Live
  acceptance comes from steps 6 and 7.
