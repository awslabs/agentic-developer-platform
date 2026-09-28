# dsh experiment manifests — issue #4188

> **NOT PRODUCTION INFRASTRUCTURE. NOT APPLIED BY ANY CI WORKFLOW.**
>
> Deliberately outside every module's infra path so no `terraform apply` and no
> deploy workflow can pick these up. Nothing here is wired into dispatch, the
> gateway, agent-context, or any queue.

These are the isolation manifests for the gated experiment designed in
[`docs/spikes/spike-4188-dsh-experiment.md`](../../docs/spikes/spike-4188-dsh-experiment.md),
which specifies the experiment from
[`docs/research/deepseek-harness-fit-assessment.md`](../../docs/research/deepseek-harness-fit-assessment.md) §6.

**The experiment is human-gated and has not been run.** Read the spike design
before applying anything here — in particular §4 (the test-5 durability substrate,
which these manifests deliberately do *not* decide for you) and §5 (the five
pre-run isolation gates, which must pass **before** the harness receives a model
credential).

## What is here

| File | Purpose |
|---|---|
| `00-namespace.yaml` | `dsh-experiment` namespace, ResourceQuota, LimitRange |
| `01-serviceaccount.yaml` | SA with **no IRSA annotation** and no RBAC |
| `02-networkpolicy.yaml` | Default-deny ingress **and** egress, plus a minimal allow-list |

## What is deliberately absent

- **No pod/Job manifest for the harness.** The image must be built from a pinned
  upstream SHA into our own ECR first (spike §3.7), and the pod shape depends on
  the substrate decision in spike §4. Shipping a ready-to-run pod spec here would
  invite someone to `kubectl apply` third-party code with documented security
  findings before the gates in §5 have been run.
- **No IRSA role.** The harness container gets none by design. The sigv4 sidecar
  needs `execute-api:Invoke` and a DynamoDB agent-registry entry (spike §3.4) —
  created out-of-band for the run and destroyed at teardown, not committed here.
- **No credentials of any kind.**

## Applying (only after the human gate)

```bash
kubectl apply -f experiments/dsh-4188/00-namespace.yaml
kubectl apply -f experiments/dsh-4188/01-serviceaccount.yaml
kubectl apply -f experiments/dsh-4188/02-networkpolicy.yaml
```

⚠️ **`kubectl get networkpolicy` succeeding is NOT evidence that traffic is
filtered.** This cluster is EKS Auto Mode and there is no evidence in the tree
that a network-policy agent is active — the same caveat `agent-context`'s own
policy documents at `manifests/networkpolicy.yaml:22-27`. Gate G2 in spike §5
(observe that telemetry egress is actually blocked) is what establishes
containment. If G2 fails, do not run the experiment.

## Teardown

```bash
kubectl delete namespace dsh-experiment
kubectl get all -n dsh-experiment          # must return nothing
kubectl get ns dsh-experiment              # must be NotFound
```

Teardown is a deliverable, not an intention — see spike §8 for the full list
(credential revocation proven by a failing call, agent deregistration, ECR image,
S3 checkpoint prefix, gVisor node group back to zero).
