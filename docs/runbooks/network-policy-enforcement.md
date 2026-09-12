# Runbook: Enabling NetworkPolicy Enforcement

Covers turning NetworkPolicy enforcement on for an EKS Auto Mode cluster: why
the ordering matters, the exact apply sequence, how to verify enforcement is
real, and how to roll back.

**Issue:** #4999 · **Blocked evaluation:** #3967 (check W1-04) · **Owning story:** #3960

---

## Section 1 — What was wrong, and why it was invisible

A Kubernetes NetworkPolicy is only a *declaration*. Something has to read each
policy, work out which pods it selects, and program the dataplane to enforce it.
On EKS that component is the VPC CNI's network-policy controller, and **EKS Auto
Mode ships it disabled**. Nothing in this repo's Terraform asked for it.

The consequence: every NetworkPolicy in the cluster was accepted by the API
server, appeared correct in `kubectl get networkpolicy`, and enforced nothing.
Four policies were affected — `adp-agents/default-deny-egress`,
`adp-agents/agent-scaledjob-egress`, `adp-agents/agent-control-listener-ingress`
and `agent-context/context-mcp-ingress`. Two of them had been in place for
months, so the worker egress restriction that operators and prior security
reviews believed was constraining the agent sandbox was not constraining
anything.

This failure mode is silent in both directions, which is what let it survive:
an unenforced policy never errors, and `kubectl get networkpolicy` looks
identical whether enforcement exists or not. The tell is `policyendpoints`:

```bash
kubectl get policyendpoints -A     # empty  → NOT enforced
kubectl get networkpolicy -A       # shows policies either way — not a signal
```

Evaluation #3967's W1-04 check measured the effect directly: a plain deny-all
ingress policy on the probe target, labels confirmed matching the `podSelector`,
and a non-gateway caller still got HTTP 200 after 45 seconds.

**Not the missing step:** the default NodeClass reports
`networkPolicy: DefaultAllow`. That is the enforcement *mode* (default-allow
until a policy selects a pod — standard Kubernetes semantics), not a
disablement. Do not mutate the shared NodeClass or `compute_config`.

---

## Section 2 — Why the order is load-bearing

Enabling enforcement is **not additive**. It makes every policy already in the
cluster take effect at the same moment. Any pod that is selected by a deny
policy but matched by no allow policy loses that traffic immediately.

In `adp-agents` there was exactly one such pod. `default-deny-egress` uses
`pod_selector {}` — every pod in the namespace — and the ADOT collector
(`adot-collector`, the sink for all agent traces, metrics and logs) matched no
allow policy. Its exporters (`awsxray`, `awsemf`, `awscloudwatchlogs`) are all
HTTPS calls to regional AWS endpoints needing DNS and STS first.

Enabling enforcement before the collector had an egress policy would have cut
all agent telemetry — and it would have failed the way blocked egress always
fails: **no pod restart, no CrashLoopBackOff, no error surfaced to any user**.
Just traces and metrics that quietly stop arriving. That is the whole reason
this is a two-step procedure rather than a flag flip.

So: **the ADOT egress policy applies first, enforcement second, in separate
applies.**

---

## Section 3 — Ordered enablement procedure

### Step 1 — Apply the ADOT collector egress policy (zero behavioural risk)

The policy lives in
`modules/agent-factory/webhook-ingress/infra/scaledjob-netpol.tf`
(`kubernetes_network_policy.adot_collector_egress`).

`webhook-ingress-deploy.yml` applies this **automatically on merge to main**
when files under `modules/agent-factory/webhook-ingress/infra/` change. To
apply deliberately instead, dispatch that workflow.

While enforcement is still off this step changes no traffic whatsoever — it only
creates an object. Confirm it exists:

```bash
export AWS_PROFILE=<profile>
aws eks update-kubeconfig --name adp-dev-eks-cluster --region us-east-1

kubectl get networkpolicy -n adp-agents adot-collector-egress
kubectl describe networkpolicy -n adp-agents adot-collector-egress
# Expect: Allowing egress traffic on ports 53/UDP, 53/TCP, 443/TCP
```

Verify the selector actually matches the running collector — a policy whose
selector matches nothing grants nothing, and that is indistinguishable from a
correct policy until enforcement starts:

```bash
kubectl get pods -n adp-agents -l app.kubernetes.io/name=adot-collector
# Must return at least one Running pod when enable_agent_otel = true.
```

### Step 2 — Confirm the worker allowlist is complete

`agent-scaledjob-egress` must cover DNS 53 (TCP+UDP), 443, OTLP 4317 to the
collector, and MCP 5100 to `agent-context`:

```bash
kubectl describe networkpolicy -n adp-agents agent-scaledjob-egress
```

**If Step 1 or Step 2 cannot be verified, stop. Do not proceed to Step 3.**

### Step 3 — Enable the controller

Set `enable_network_policy_controller = true` in the environment's tfvars
(already set for dev in `environments/dev/platform.tfvars`), then apply
`platform/infra`.

`platform-infra-apply.yml` is **manual-only** (`workflow_dispatch`) — merging a
PR never auto-applies platform infra. This is what makes the ordering safe by
construction rather than by convention: the policy apply fires on merge, while
enforcement requires a separate deliberate dispatch. The two cannot race.

Actions → **Platform Infra Apply** → Run workflow.

> **Read the plan before confirming.** As of this writing the platform plan also
> carries unrelated ECR encryption drift (#5003) — the code declares KMS, the
> live repositories are AES256 — so the plan wants to **replace** three image
> repositories. The only resource this change needs is
> `module.eks.kubernetes_config_map.amazon_vpc_cni[0]`. If the plan shows ECR
> destroys or replacements, resolve #5003 separately. **Do not** pass
> `confirm_destructive_apply=yes` just to get this ConfigMap applied.

Two things to get right if you meet that plan:

- **The likely outcome is a failed partial apply, not image deletion.**
  `force_delete` is unset on those repositories, so ECR refuses to delete a
  non-empty one and the apply stops with an error. That failure is the safety
  net working. **Adding `force_delete` or emptying the repositories is not the
  workaround** — that is exactly what converts a safe failure into ~712 deleted
  images, including digests that running workloads are pulling.
- **The destroy gate will not stop it for you.** Per #5002 the destroy-safety
  gate counts destroys but not replacements, so a replace-only plan can apply
  without the approval you would expect to be required. Read the plan yourself
  rather than relying on the gate to refuse.

To apply only what this change needs, without the collateral:

```bash
terraform apply -target=module.eks.kubernetes_config_map.amazon_vpc_cni
```

That leaves the ECR drift untouched for #5003 to resolve on its own terms. Note
that a `-target` apply is deliberately narrow: it skips the rest of the module,
so run a normal plan afterwards to confirm nothing else was pending.

Provenance of the drift is **unknown** — do not attribute it to a specific
commit. A shallow clone makes its graft root appear to introduce every file in
the repository, which is misleading rather than informative here.

### Step 4 — Verify the controller actually reconciled

This is the step that distinguishes "flag set" from "enforcement working". The
controller must translate each policy into `PolicyEndpoint` objects:

```bash
kubectl get configmap -n kube-system amazon-vpc-cni -o yaml
# Expect: enable-network-policy-controller: "true"

kubectl get policyendpoints -A
# Expect: NON-EMPTY, with endpoints covering all existing policies.
```

Smoke test (single command):

```bash
kubectl get policyendpoints -A --no-headers | wc -l   # expect > 0
```

If this stays empty for more than a few minutes, enforcement is **not** active —
treat the cluster as unenforced and investigate before relying on any policy.

### Step 5 — Positive and negative probes

Use **Job- or Deployment-controlled** fixtures, never bare pods. AWS documents
that standalone pods can be unreliable for policy enforcement, so a bare-pod
result is not evidence in either direction — the earlier
`adp-eval3967-polcheck-target` probe had no `ownerReferences`, which is why its
result could not be trusted.

| Probe | Expected |
|---|---|
| Non-gateway pod → agent control port 8770 | **Fails / times out** (the W1-04 boundary) |
| Gateway pod → agent control port 8770 | Succeeds |
| Worker fixture → DNS, 443, 4317, 5100 | All succeed |
| Fresh span/metric/log in `/adp/dev/agent-factory/otel/*` | Lands **after** the enforcement timestamp |

The telemetry check is the one that catches the failure mode from Section 2.
Check the timestamp, not just presence — buffered data from before enablement
will still be there and will look like success.

---

## Section 4 — Rollback

```bash
# Set in the environment's tfvars, then dispatch Platform Infra Apply:
enable_network_policy_controller = false
```

Enforcement stops and the previous behaviour returns. Two things to know:

- The ADOT egress policy is **safe to leave in place** — it is additive and
  harmless whether enforcement is on or off. Do not revert it.
- Rollback restores the *unenforced* state, so worker egress and the control
  listener boundary become nominal again and #3967 is blocked again. That is the
  accepted trade if telemetry or worker paths regress.

**Never widen a policy to make a probe pass.** If the negative probe in Step 5
succeeds when it should fail, that is a real isolation finding — investigate it.
Loosening the boundary would make Wave 1 pass falsely and leave Waves 2–4 built
on a foundation that was never isolated.

---

## Section 5 — Enabling in another environment

Each environment enables separately and deliberately. Before setting the flag,
repeat the Section 2 audit **for that cluster**: list every NetworkPolicy, and
for each pod selected by a deny with no matching allow, add the allow first.
`adp-agents` needed one such fix (the ADOT collector); another environment or
namespace may need others.

```bash
kubectl get networkpolicy -A
kubectl get pods -A --show-labels     # cross-check selectors against real pods
```

---

## Related

| Reference | Purpose |
|---|---|
| [AWS: Auto Mode network policy](https://docs.aws.amazon.com/eks/latest/userguide/auto-net-pol.html) | The documented ConfigMap enablement |
| [AWS: CNI network policy](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy.html) | Pod/controller enforcement constraints (bare-pod caveat) |
| [Live Run-Control Evaluation](./agent-control-evaluation.md) | The Wave 1 evaluation this unblocks |
| `platform/infra/modules/eks/main.tf` | `kubernetes_config_map.amazon_vpc_cni` |
| `modules/agent-factory/webhook-ingress/infra/scaledjob-netpol.tf` | The four namespace policies + the ADOT egress fix |
