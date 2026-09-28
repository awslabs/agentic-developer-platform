# #4010 — Internal-plane separation at the load balancer

**Status:** implemented
**Issue:** [#4010](https://github.com/aws-e/adp/issues/4010) (A2 remainder)
**Parent:** #3985 (Group A) → sub-EPIC #3984 → EPIC #615
**Siblings:** #3996 (edge header strip), #4000 (app identity-reject), #4007 (app-side scope/tenant checks)

## What this closes

A2 was approved with the requirement that the internal control plane be
unreachable from the CloudFront edge **by routing, not merely by header
hygiene**. The other three A2 items shipped in #4007; this note covers the
routing item.

The pre-existing controls are all app- or edge-function-level:

- **#3996** — the CloudFront function on `/api/*` deletes `x-caller-identity`,
  `x-amzn-iam-user-arn`, `x-amzn-requestcontext`, `x-auth-source`,
  `x-internal-api-key` and all `x-agent-*`.
- **#4000 / #4007** — `verify_internal_or_irsa` rejects unidentified callers, plus
  an internal-plane scope allowlist on the IRSA path.

Those are sound, but they mean internal-plane unreachability rests on a **single
edge control**. Any future cache-behavior or function edit that drops the header
strip would silently re-expose the internal plane. This change adds the
routing-layer backstop underneath.

> This was **defence-in-depth, not a live hole.** An edge request to
> `/api/internal/v1/...` already arrived with no identity and no secret and was
> rejected by the app.

## Why the issue's mechanism turned out to be unexpressible

The issue specified putting the API-GW VPC-Link integrations on a **separate ALB
listener port** from the CloudFront-facing port. Spikes against the live dev
account (`879318057152`, `us-east-1`) produced two findings that block that exact
shape, and one that opens a better one.

### Finding 1 — an out-of-band deny rule can never be ordered before the catch-all

The issue anticipated needing "an explicit `fixed-response` 403 rule ordered
before the catch-all `/`", since a missing rule falls through to the listener
default rather than denying. That ordering is **not achievable** from Terraform
or the CLI. The Auto Mode controller places its own catch-all `/*` forward at ALB
rule **priority 1**, and ALB priorities start at 1:

| Attempt against the live listener | Result |
|---|---|
| `create-rule --priority 1` | `PriorityInUse: Priority '1' is currently in use` |
| `create-rule --priority 0` | `ParamValidation: valid min value: 1` |

There is no priority below the controller's catch-all. Any out-of-band rule lands
at ≥2, is evaluated **after** the catch-all has already matched and forwarded,
and is therefore a **silent no-op** — exactly the "deny rule ordered after the
catch-all → internal plane still edge-reachable" row in the issue's own impact
table.

**Consequence:** the deny must be expressed *through the Ingress*, so the
controller itself orders it (by path specificity — `/internal` beats `/`).
`modules/gateway/infra/modules/alb/` remains dead code; do not revive it for this.

Separately tested: an out-of-band rule at priority 50 survived a ~5-minute soak
without being pruned by the controller. So the answer to the issue's "assume it
prunes until tested" is *probably not* — but it is moot, because ordering is the
blocker, not pruning.

### Finding 2 — per-listener rule scoping needs two Ingresses, which replaces the ALB

The `listen-ports` merge wording ("You can define different listen-ports per
Ingress, Ingress rules will only impact the ports defined for that Ingress") is
about **different Ingresses within a group**. A *single* Ingress declaring
`[{"HTTP":80},{"HTTP":8081}]` gets the **same rules replicated onto both**
listeners — no separation at all.

Getting different rules per listener therefore needs two Ingresses in one
IngressGroup. On EKS Auto Mode, `alb.ingress.kubernetes.io/group.name` is
unsupported ("Specify groups in IngressClass only"), so grouping must move to
`IngressClassParams.spec.group.name`. Confirmed live (spike 1):

```
$ kubectl explain ingressclassparams.spec.group
GROUP: eks.amazonaws.com   KIND: IngressClassParams   VERSION: v1
FIELD: group <Object>
  FIELDS: name <string> -required-
```

But changing group identity **creates a new ALB and deletes the old one**,
ignoring deletion protection — forcing a CloudFront-VPC-origin + API-GW + SSM
re-wire of the live edge path. That is the worst row in the impact table ("full
gateway outage until ... re-pointed at the new DNS").

Also confirmed: today's `group.name: bedrockgw` annotation **is already a no-op**.
The live ALB is `k8s-adpgatew-bedrockg-8c0085afd5`, tagged
`ingress.eks.amazonaws.com/stack: adp-gateway/bedrockgateway` — the implicit
single-Ingress pattern, not a `bedrockgw` group.

### Finding 3 — the REST `uri` port DOES route (the issue's doc citation is wrong)

Design point 4 of the issue held that for REST private integrations "the URI is
not used for routing", making per-listener targeting an HTTP-API-only feature.
**This is false.** Tested with two resources identical except the `uri` port,
both with `integrationTarget` = the **load balancer** ARN over VPC Link `qmovr6`:

| Stage | `/p80` (uri `:80`) | `/p8081` (uri `:8081`) |
|---|---|---|
| ALB has only listener 80 | `200 healthy` | `500`, **11.2s connect timeout** |
| + listener 8081 created, SGs closed | `200 healthy` | `500`, 10.8s timeout |
| + ALB SG ingress **and** VPC-Link SG egress opened on 8081 | `200 healthy` | `200 healthy` |
| + listener 8081 default action → `fixed-response 418` | `200 healthy` | **`418 LANDED-ON-LISTENER-8081`** |

The last row proves the response came from **listener 8081's own default
action** — the port in `uri` genuinely selects the listener. The progression also
shows the failures were real TCP connect timeouts, never a silent fallback to 80.

Resolved alongside it, the doc conflict the issue flagged about
`integrationTarget`:

| `integrationTarget` | Result |
|---|---|
| **listener** ARN | `BadRequestException: ... is not a valid ALB or NLB arn` |
| **load balancer** ARN | accepted |

So the AWS API/CLI/boto3/CFN reference wording ("The ALB or NLB *listener* to
send the request to") is a **documentation error**; the worked examples, console,
and Terraform are right. `integrationTarget` takes a load balancer ARN on REST.

## The shape that shipped: a separate ALB, not a separate listener

Findings 1 and 2 rule out the literal mechanism without replacing the live edge
ALB. But because on Auto Mode **one Ingress == one ALB** (no group), a dedicated
internal-plane Ingress gets its **own** ALB while the edge Ingress keeps its
namespace/name — and therefore its stack identity, its ALB, and its DNS name.

**No ALB replacement, no DNS re-wire, no edge outage window.**

The separation is also *stronger* than a second listener would have been:
CloudFront has **no VPC origin** for the internal ALB, so the internal plane is
not merely denied at the edge — it is not addressable from it.

```
Edge:     CloudFront ──VPC origin──> edge ALB (ingress.yaml)
                                     /internal -> fixed-response 403 (never reaches a pod)
Internal: API GW /internal/{proxy+} (AWS_IAM/SigV4)
                  ──VPC Link v2──>   internal-plane ALB (ingress-internal.yaml) -> pod
Callback: pod ──ClusterIP Service──> pod   (never traverses either ALB)
```

### Why the edge deny rule is still needed

The edge ALB is CloudFront's origin, so anything the edge can request arrives
there. And the CloudFront function **strips the `/api` prefix**
(`cloudfront/main.tf:113`), so an edge request to `/api/internal/v1/...` reaches
the edge ALB as `/internal/v1/...` — byte-identical to a legitimate internal
call. Path is the only discriminator available at that ALB, hence the explicit
403.

### Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Secret-header discriminator | Explicitly rejected in the approved design (re-couples layers) |
| Blanket app-middleware `/internal/*` reject | Explicitly rejected — would 403 the ClusterIP ingestion status callback and halt ingestion platform-wide |
| `conditions.*` `source-ip` scoped to VPC-Link subnets (the issue's fallback) | **Would not actually separate the planes.** CloudFront's ENIs (`10.0.11.75`, `10.0.10.219`) and API Gateway's VPC-Link ENIs (`10.0.11.48`, `10.0.10.6`) sit in the **same two private subnets** (`10.0.10.0/24`, `10.0.11.0/24`) |
| Terraform `aws_lb_listener_rule` deny on the existing listener | Finding 1 — cannot be ordered before the controller's priority-1 catch-all; silent no-op |

The ClusterIP status callback is untouched by design: it goes pod → Service →
pod and never reaches an ALB, so no listener rule can catch it.

## Cost

One extra internal ALB (~$16/month) rather than the one extra listener the issue
estimated. This is the deliberate trade for not replacing the live edge ALB. No
new DB rows, no new compute.

## Rollout ordering

> **RETRACTION.** An earlier revision of this note (and of PR #4138) claimed
> "there is no flag-day and no window where `/internal/{proxy+}` points at
> nothing," reasoning only about the Terraform layer. **That claim was wrong**,
> and it is retracted. The Terraform fallback is real, but the *first* revision of
> this change also shipped the `/internal` → 403 deny inside
> `k8s/ingress.yaml`, which `gateway-deploy.yml` applies unconditionally on
> merge — with no knowledge of Terraform state. On any established environment
> that took the internal control plane down. The mechanism is recorded below,
> because the trap is not obvious and is easy to reintroduce.

### The outage the first revision would have caused

1. Merge → the `k8s/*.yaml` loop applies `ingress.yaml`, so the **edge ALB starts
   403-ing `/internal`** immediately.
2. `gateway-deploy.yml`'s "Wire ALB" step hit `exit 0` on the cached edge ALB, so
   **internal-plane discovery never ran** and the internal-plane SSM params stayed
   empty.
3. The `gateway-infra-apply` trigger is gated on `newly_wired == 'true'`, so it
   was **skipped** — `internal_plane_alb_dns` stayed empty and the Terraform
   fallback kept `/internal/{proxy+}` pointed at the **edge ALB**.
4. Net effect: every SigV4 `/internal/{proxy+}` call — agent provenance writes,
   credential paths — got a **403 from the edge ALB, indefinitely**, until a human
   ran `wire-gateway-alb.sh --apply` by hand.

A second, independent gap made that permanent rather than self-healing:
**`gateway-infra-apply.yml` never passed the internal-plane vars at all.** Its
plan/apply `env:` block set `TF_VAR_internal_alb_*` but had no
`TF_VAR_internal_plane_alb_*`, even though `wire-gateway-alb.sh --no-wait` was
already emitting those outputs. So even a *successfully triggered* re-apply would
have re-applied with the vars empty and left the integration on the edge ALB. The
repoint was reachable only via `wire-gateway-alb.sh --apply` or `deploy-all.sh` —
never via the merge path.

### The invariant

> The edge ALB must never deny `/internal` while the API Gateway
> `/internal/{proxy+}` integration still targets that same edge ALB.

The deny and the repoint are in different layers (Kubernetes vs. Terraform), so
nothing about applying a manifest can make them simultaneous. Instead the deny is
made **strictly last**, and conditional on the repoint being observable:

1. **Merge** — `k8s/*.yaml` is applied. `ingress.yaml` no longer contains the
   deny, so the edge behavior is **unchanged**. `ingress-internal.yaml` is applied
   and the controller begins provisioning the internal ALB (~2–3 min).
2. **Discovery** — `wire-gateway-alb.sh` finds the internal ALB by its
   `ingress.eks.amazonaws.com/stack` tag and caches it to SSM. This now runs even
   when the edge ALB is already cached (the `exit 0` short-circuit was the root
   cause of step 2 in the outage above).
3. **Repoint** — `gateway-infra-apply.yml` re-applies with
   `TF_VAR_internal_plane_alb_*` populated, moving `/internal/{proxy+}` to the
   internal ALB, and redeploys the stage.
4. **Deny** — only now does
   `modules/gateway/scripts/apply-internal-plane-deny.sh` apply
   `k8s/patches/edge-internal-deny.yaml`. Its precondition is read from the
   **live integration URI**, not from SSM or Terraform state, so it confirms the
   repoint actually *landed* rather than that it was requested.

If the precondition does not hold the script **skips and exits 0**, leaving the
internal plane working exactly as before; a later deploy applies the deny once the
repoint has landed. The inverse ordering can therefore never be reached by the
automated path, and `--verify` fails loudly if it is ever reached by any other
means.

Because the re-apply in step 3 is dispatched asynchronously (`gh workflow run`),
the deploy run that *first* discovers the internal ALB will normally still be
waiting on the repoint, and will correctly skip the deny. The deny lands on the
following deploy. This is expected, not a failure.

### Why the deny lives in `k8s/patches/`, not `k8s/`

Both apply paths — `gateway-deploy.yml` and `deploy-all.sh` — glob `k8s/*.yaml`
**non-recursively** and apply everything they find, unconditionally. Any manifest
in `k8s/` is therefore ungateable by construction. `k8s/patches/` is outside that
glob, which is what makes the gating possible at all. A partial Ingress left in
`k8s/` would additionally be applied blind and three-way-merge away the real
`spec.rules`.

Note that the patch restates the catch-all `/` rule: a strategic-merge patch
replaces `spec.rules` wholesale rather than merging into it. If `ingress.yaml`'s
catch-all ever changes, the patch must change with it.

### The discovery hazard this introduced, and the fix

`wire-gateway-alb.sh` previously took the **first `Scheme==internal` load
balancer in the account**. That was unambiguous with one gateway ALB and is a
coin-flip with two. Picking the internal-plane ALB for `internal_alb_dns` would
point CloudFront's VPC origin and the public `/{proxy+}` route at an ALB that
only serves `/internal` — **a full gateway outage.**

Discovery now matches on the `ingress.eks.amazonaws.com/stack` tag
(`find_alb_by_stack()`), which is the only deterministic discriminator, keeping
the old heuristics as a fallback. The script also **fails loudly** if the two
resolve to the same ALB, since that would mean the separation silently does not
exist.

## Files

| File | Change |
|---|---|
| `modules/gateway/k8s/ingress-internal.yaml` | **new** — internal-plane Ingress → its own internal ALB |
| `modules/gateway/k8s/patches/edge-internal-deny.yaml` | **new** — the `/internal` → 403 deny, as a **gated** patch (deliberately outside the `k8s/*.yaml` glob) |
| `modules/gateway/scripts/apply-internal-plane-deny.sh` | **new** — applies the deny only after a confirmed repoint; `--verify` asserts the invariant; `--remove` rolls back |
| `modules/gateway/k8s/ingress.yaml` | documented the no-op `group.name`, and why the deny is **not** here |
| `modules/gateway/infra/modules/api-gateway/main.tf` | `/internal/{proxy+}` → internal-plane ALB w/ fallback; VPC-Link SG egress + reciprocal ingress |
| `modules/gateway/infra/modules/api-gateway/variables.tf` | new `internal_plane_alb_*` vars |
| `modules/gateway/infra/{main,variables}.tf` | plumbing |
| `platform/scripts/wire-gateway-alb.sh` | tag-based discovery + internal-plane ALB discovery/SSM/exports; applies the deny last under `--apply` |
| `.github/workflows/gateway-deploy.yml` | always run internal-plane discovery (removed the cached-ALB `exit 0`); trigger the repoint; gated deny step + `--verify` assertion |
| `.github/workflows/gateway-infra-apply.yml` | pass `TF_VAR_internal_plane_alb_*` — without this the repoint could never happen in CI |
| `platform/scripts/deploy-all.sh` | pass the internal-plane vars; apply the deny last |

## Validation

**⚠️ ELB config changes take ~30s to propagate.** During the spike, the first
requests after `modify-listener` still returned the *old* response. Any smoke
test here must retry for ~60s before believing a result, or it will read a stale
pass. This is precisely the "silent failure — must be tested, not assumed" risk
the issue called out.

Negative — edge must not reach the internal plane (expect **403**):

```bash
for i in $(seq 1 12); do
  curl -si https://<cloudfront-domain>/api/internal/v1/provenance -X POST -d '{}' | head -1
  sleep 5
done
```

Positive — the ClusterIP status callback must still return 200:

```bash
kubectl -n adp-gateway exec deploy/bedrockgateway -- \
  curl -si -X POST http://bedrockgateway/internal/v1/knowledge-assets/status-callback \
    -H "X-Internal-Api-Key: $KEY" \
    -d '{"asset_id":"<uuid>","status":"indexing"}'
```

Positive — SigV4 `/internal/{proxy+}` from the agent-worker role still returns 200.

**The invariant assertion** — this is the check that would have caught the outage
described under "Rollout ordering", and it runs automatically at the end of every
`gateway-deploy.yml` run:

```bash
bash modules/gateway/scripts/apply-internal-plane-deny.sh --verify
```

It fails if the edge ALB denies `/internal` while the API-GW integration still
targets that same ALB. Note the negative smoke test above is **only** meaningful
once this reports that the deny is live — before that, `/api/internal/...` is
rejected by the app (#4000/#4007) rather than by routing, which looks similar from
outside but is not what this change is asserting.

Confirm the two ALBs are genuinely distinct:

```bash
aws elbv2 describe-tags --resource-arns $(
  aws elbv2 describe-load-balancers \
    --query 'LoadBalancers[?Scheme==`internal`].LoadBalancerArn' --output text | tr '\t' ' '
) --query "TagDescriptions[?Tags[?Key=='ingress.eks.amazonaws.com/stack']].{ARN:ResourceArn,Stack:Tags[?Key=='ingress.eks.amazonaws.com/stack']|[0].Value}"
```

Regression: CloudFront `/api/*` and `/.well-known/*` still serve; GitLab
`/gitlab/*` VPC origin intact; agent provenance writes and credential routes
still work over SigV4; ingestion status callbacks still land.

### The `enable_vpc_origin` trap

`gateway-infra-apply.yml` derives `TF_VAR_enable_vpc_origin` from a **live ALB
probe**. Any plan must show **UPDATE, not delete**, of the CloudFront `/api/*`
and `/.well-known/*` behaviors, and must not destroy the live GitLab VPC origin.
Read the plan before approving.

## Rollback

**Order matters, and it is the reverse of the rollout.** Remove the deny *before*
moving the integration back, or you land in exactly the outage state the invariant
forbids.

1. **Remove the edge deny first** — this alone restores the internal plane, in
   ~30s (ELB propagation), with no Terraform involved:
   ```bash
   bash modules/gateway/scripts/apply-internal-plane-deny.sh --remove
   ```
   This is the emergency lever: it does not depend on Terraform state, a live
   probe, or CI. If internal calls are 403-ing, run this.
2. **Then** clear `internal_plane_alb_*` (or revert the Terraform) so
   `/internal/{proxy+}` falls back to the edge ALB, and re-apply.
3. Deleting `ingress-internal.yaml` deletes only the internal-plane ALB. **The
   edge ALB and its DNS are untouched by this change** — that is the main reason
   the separate-ALB shape was chosen over regrouping.

Reverting the *manifests* alone is not sufficient and not the fast path: the deny
lives on the live Ingress object, so it persists until step 1 removes it (or until
a re-apply of `ingress.yaml` resets `spec.rules` — which leaves the now-dangling
`actions.deny-internal` annotation behind, hence the explicit `--remove`).

Record the pre-change edge ALB DNS/ARN so rollback never depends on a live probe:

```
ALB : k8s-adpgatew-bedrockg-8c0085afd5
DNS : internal-k8s-adpgatew-bedrockg-8c0085afd5-428441231.us-east-1.elb.amazonaws.com
```

## Follow-up

The internal-plane ALB currently listens on port 80, matching the edge ALB. Now
that finding 3 has established the `uri` port really does route, a future
hardening step could move it to a non-80 port for extra clarity. It would require
opening **both** the ALB SG ingress *and* the VPC-Link SG egress on that port —
the spike's second row shows that opening only one side yields a silent ~10s
timeout then 503.
