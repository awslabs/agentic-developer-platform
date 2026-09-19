# Superplane Kubernetes manifests

Applied by [`.github/workflows/superplane-k8s-deploy.yml`](../../../../.github/workflows/superplane-k8s-deploy.yml).
Issue #5042 (U3), EPIC #4910.

## What is deployable here, and what is not

| Component | State | Why |
|---|---|---|
| SkyPilot API service | **Deployable** | `releases/superplane.lock.yaml` resolves `skypilot-api` to `sha256:de41a5c6…` (`berkeleyskypilot/skypilot:0.12.0`) |
| `superplane-api` | Not deployable | `pending_images`, no digest — no build has run yet |
| `superplane-controller` | Not deployable | same |
| `superplane-platform-monitor` | Not deployable | same |

The three application images have no digest because none has been built. What changed with
U22 (#5326) is *why*: their source used to be unreachable — a repository ADP cannot read —
and is now maintained in this repository under `../src/`, so the build lanes can run. Source
availability is not a digest, though, and this lane needs the digest.

The rollout lane's preflight fails closed on any manifest referencing them — a placeholder
digest would look authoritative and be believed, which is worse than a visibly floating tag.

The transferred components ship their own upstream `deploy/` manifests. Those are **not**
these manifests and are not applied by this lane: they carry upstream's account id and
`:latest` image tags, and are inventoried as read-only evidence in
`../src/TRANSFER-MANIFEST.md`. Reconciling what the accepted topology needs from them into
this directory is U3's work, so that this lane keeps one reviewed source of manifests
instead of two.

Because three of four components are unbuildable, this lane is **dispatch-only**. A
push-triggered rollout would be an automatic partial deploy.

## Files

| File | Contents |
|---|---|
| `00-namespace.yaml` | The domain namespace, `pod-security.kubernetes.io/enforce: restricted` |
| `10-skypilot-rbac.yaml` | ServiceAccount (IRSA-annotated), namespaced Role, RoleBinding |
| `20-skypilot-config.yaml` | `~/.sky/config.yaml` — U2's pinned `allowed_clouds`, Postgres backend |
| `30-skypilot-networkpolicy.yaml` | default-deny plus scoped ingress/egress allows |
| `40-skypilot-api.yaml` | ClusterIP Service (`:46580`) and the single-replica Deployment |
| `rollback.sh` | Rollback to a previously deployed digest, or teardown |

Every `REPLACE_WITH_*` placeholder is substituted from an SSM parameter that
`infra/control-plane/config.tf` publishes. None is a workflow literal: Terraform already
knows these values, and a second copy in the workflow can disagree with the first with
nothing to detect it.

## Claims these manifests do NOT make

### Network isolation is not established by this directory

A NetworkPolicy is a *declaration*. On EKS the VPC CNI's network-policy controller must
read it and program the dataplane, and **EKS Auto Mode ships that controller disabled**. On
a cluster without it these objects are accepted, appear in `kubectl get networkpolicy`, and
enforce nothing — silently, in both directions.

Enabling it is a **platform-owned prerequisite, [#4999](../../../../docs/runbooks/network-policy-enforcement.md),
which is still open.** It is not this lane's to enable: `platform-infra-apply.yml` is
manual-dispatch-only, and enabling enforcement makes every policy already in the cluster
take effect at the same moment — the ordered procedure in that runbook exists because doing
it carelessly once cut all agent telemetry with no pod restart and no error.

So: **a rendered NetworkPolicy, and a successful `kubectl apply`, are not an isolation
claim.** `check_rendered_manifests.py` checks presence and says so in its own output. The
evidence that would establish enforcement is

```bash
kubectl get policyendpoints -A     # empty -> NOT enforced
```

plus the positive/negative probes in Section 5 of the runbook. Both belong to the gated live
acceptance, which this story is not authorized to run.

The policies are declared now anyway, because they must exist and be correct *before*
enforcement is switched on — shipping the allow rules alongside the deny is what avoids
repeating the telemetry incident.

### No database property is claimed

**Decision 2 (shared instance vs. separate instance) is unresolved.** Nothing here
provisions a database, and declaring either shape would decide it by default. The
connection arrives as a reference (a Kubernetes Secret seeded out of band from the Secrets
Manager secret named by `var.database_secret_name`). No isolation, backup, retention or
restore property is claimed about whatever it points at.

### GPU scheduling is not enabled

`workspace_cluster_context` is empty in dev; Terraform publishes the sentinel `none` so
"unset" cannot be read as "use the current cluster". The Role in `10-skypilot-rbac.yaml`
grants **no pod-create rights**, so a Kubernetes-cloud launch fails with a permission error
rather than provisioning on the ADP *management* cluster. That ordering is the point: fail
on a missing credential, never succeed against the wrong cluster.

A live GPU run is not authorized by this story. U19 owns the SkyPilot resource/state
handover.

## Prerequisites before a rollout can succeed

1. `superplane-infra-apply.yml` has run — the lane reads the SSM parameters it publishes,
   and a missing parameter means the IAM roles these pods authenticate as do not exist.
2. The Kubernetes Secret `skypilot-api-db` exists in the SkyPilot namespace with key
   `connection-uri`, seeded from the Secrets Manager secret named by
   `var.database_secret_name`. The pod declares `optional: false`, so it will not start
   with an empty connection string and silently fall back to SkyPilot's SQLite default —
   which would discard cluster state on every restart.

## Rollback

```bash
./rollback.sh --environment dev --to-digest sha256:<previously-deployed>   # revert
./rollback.sh --environment dev --teardown                                # remove
```

The script requires the target digest explicitly and verifies it against the cluster's own
rollout history before acting. See its header for why "roll back to the previous revision"
is not a safe default here.
