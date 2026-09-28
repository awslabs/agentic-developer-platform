# Capacity retirement in the Superplane controllers

This note is for operators. It explains how the controllers tell a **deliberate
retirement** apart from an **accidental deletion**, and what happens when a
teardown cannot be confirmed against the cloud provider.

## Deliberate retirement versus accidental deletion

Both cases look similar from inside the cluster: a GPU node stops being Ready and
its pods stop running. The controllers must react in opposite ways, so the
difference is recorded explicitly rather than inferred.

| | Accidental deletion / failure | Deliberate retirement |
|---|---|---|
| What it is | The node died on its own: hardware fault, spot reclaim, kubelet loss, someone deleted the instance out of band | An owner or operator decided this capacity should go away |
| How the controllers recognise it | Nothing marks the node — it is simply unhealthy | `status.phase: Retiring`, or the `superplane.ai/retirement` annotation carrying the human reason |
| Health monitor (`health_monitor.go`) | After 15 min unhealthy, provisions a replacement and drains the node (auto-repair) | **No repair, no replacement.** Records a `Retired` condition and stops |
| Pod watcher (`pod_watcher.go`) | Unschedulable GPU pods trigger new capacity | Retiring nodes are not counted as usable capacity, and pods annotated `superplane.ai/retirement` do not trigger provisioning |
| Provisioner (`provisioner.go`) | Pending nodes are provisioned | Retiring nodes are skipped, even while still `Pending` |
| Consolidator (`consolidator.go`) | Idle nodes are removed on TTL | Retiring and `ReleaseFailed` nodes count against the pool's `maxUnavailable` disruption budget |

Auto-repair is **not** disabled by this behaviour. Suppression is scoped to nodes
carrying a retirement marker; an accidental failure on any other node still
repairs exactly as before.

### Marking a node as retired

Either signal is sufficient, and both are honoured by every controller through
one predicate (`SuperplaneNode.IsDeliberatelyRetiring()`):

```bash
# Preferred: annotate with the reason. The annotation survives every later phase
# transition, so the intent stays readable once the node moves to Draining.
kubectl annotate superplanenode sp-node-abc \
  superplane.ai/retirement="decommissioning pool, ticket OPS-1234"
```

The annotation value is free text; any non-empty value means "retired on
purpose". It is what appears in the `Retired` condition message and in the
controller logs, so make it something a future reader can act on.

To retire the workload demand as well — so the pod watcher does not immediately
re-provision capacity for pods that are still Pending — annotate the pods with
the same key:

```bash
kubectl annotate pod my-training-job superplane.ai/retirement="job cancelled"
```

Without that, retiring the node alone is not enough: the pending pod is still
real demand and the pod watcher will honour it with a **new** node. Retirement of
capacity and retirement of demand are separate decisions and are marked
separately.

### Evidence that a repair was suppressed

The health monitor writes a condition instead of silently doing nothing:

```
Type:    Retired
Status:  True
Reason:  DeliberateRetirement
Message: Capacity deliberately retired (decommissioning pool, ticket OPS-1234);
         auto-repair suppressed and no replacement created
```

This distinguishes "no replacement exists because retirement was intended" from
"auto-repair failed". If a retired node has no `Retired` condition, the health
monitor has not observed the retirement yet.

## Confirmed teardown, and what happens when it is not confirmed

Release is only considered complete when the **provider** says the resources are
gone. An internal success status is not evidence: the call that requested
teardown can return success while the cluster, its disks or its network
attachments survive and keep billing.

The sequence for removing a node is:

1. **Cordon** the Kubernetes node (`spec.unschedulable = true`).
2. **Evict** its pods through the Eviction API, which is what makes the API
   server enforce **PodDisruptionBudgets**. Drain still respects PDBs exactly as
   before this change; nothing here bypasses them. DaemonSet pods are skipped.
3. **Tear down** the provider cluster.
4. **Re-check the provider** for that cluster.
5. Delete the Kubernetes node object.
6. Only then mark the SuperplaneNode `Terminated`.

Step 4 is the one that decides the outcome:

| Provider re-check result | Interpretation |
|---|---|
| Cluster absent | Released |
| Cluster reported `TERMINATED` | Released |
| Cluster still reported `UP` / `INIT` / running | **Not released** — resources may still be billing |
| Cluster reported `STOPPED` | **Not released** — a stopped cluster retains its disks, so storage still costs money |
| Re-check failed (expired credentials, timeout, API error) | **Unknown, which is treated as not released** |

An unreachable provider is never read as success. If credentials expire mid-run,
the release fails loudly rather than recording a cleanup that never happened.

**The re-check waits for the teardown to finish.** Asking the provider to release
a cluster is asynchronous — the call returns a request ID and the teardown runs
afterwards — so for a short window the provider legitimately still reports the
cluster as `UP` or `INIT`. The re-check therefore polls (up to 10 minutes, every
10 seconds) and only the state at the deadline is a verdict. Without that wait an
ordinary in-progress teardown would be recorded as a failed release, which would
park a healthy release in `ReleaseFailed`, consume the pool's disruption budget
(`ReleaseFailed` counts as unavailable, and `maxUnavailable` defaults to 1) and
leave the Kubernetes node object behind. A cluster still present at the deadline
is still a genuine failure — waiting defers the verdict, it does not soften it.

### `ReleaseFailed`

A node whose teardown failed or could not be confirmed goes to
`status.phase: ReleaseFailed`, **not** `Terminated`, and
`status.skypilotCluster` is **retained**. That cluster name is the only handle a
later reconciliation (or a human) has to a possibly-live GPU cluster; erasing it
would strand the resource with nothing pointing at it.

The reconcile itself returns an error naming every node that failed, so the
failure surfaces in controller logs and metrics instead of being swallowed. The
same applies during provisioning: if a launch fails and the cleanup of its
partially-created cluster cannot be confirmed, the node is `ReleaseFailed` with
the cluster name recorded, and the message says the cleanup was **not**
confirmed.

To find unresolved allocations:

```bash
kubectl get superplanenodes -A \
  -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,CLUSTER:.status.skypilotCluster,MSG:.status.message \
  | grep ReleaseFailed
```

Each row is a cluster to verify against the provider console or CLI before the
record is cleared. Do not clear a `ReleaseFailed` node by hand until the provider
confirms the resources are gone.

## Idle-node TTL

`ttlSecondsAfterEmpty` measures from `status.lastPodScheduledAt`. That field is
now written by the consolidator — it stamps the node while pods are still running
on it, and stamps it once when the node first becomes empty to start the clock.
Previously nothing wrote it, so the TTL comparison was never reached and the only
thing eventually reclaiming an idle GPU node was SkyPilot's 120-minute
`IdleMinutesToAutostop`. Idle nodes now expire on the pool's configured TTL
(default 300s).

Note that consolidation remains **opt-in** per pool
(`spec.consolidation.enabled`, default `false`); enabling it is a separate cost
decision and is unchanged here.

## Not covered by this document

Actually releasing real provider resources and observing a window in which
nothing is recreated is a live exercise against a real account. It requires a
named account, spend limit, recovery authority, deadline and cleanup owner, and
is tracked separately from the controller behaviour described above.
