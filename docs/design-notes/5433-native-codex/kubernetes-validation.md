# Kubernetes validation qualification

The shared source exporter now has local Docker and Kubernetes executors.
Kubernetes checks require a registry-qualified `repository@sha256:digest`,
frozen in the existing repository policy. The model still supplies only the
admitted check name and expected commit. Source expansion is bounded at 192 MiB;
the executor compresses the verified archive into immutable 512 KiB ConfigMap
chunks, with a 64 MiB compressed limit.

The validation Pod receives source, a writable bounded workspace and temporary
directory. It has no workload token, cloud credentials, Docker socket or host
mounts. It runs as UID 65534, with a read-only root, dropped capabilities,
RuntimeDefault seccomp, CPU/memory limits, no restart, and a deadline. Both
ingress and egress are denied. Another network policy in the namespace refuses
execution because an allow policy could override that denial.

Only nodes labelled `adp.dev/validation-isolation=v1` are eligible. Operators
must establish actual NetworkPolicy enforcement and kubelet `podPidsLimit <= 128`
before assigning that label. A namespace policy object alone is not proof of
packet filtering. The local qualification tests include a reachable control
endpoint, actual denied connection and bounded process-spawn checks.

## Cleanup and recovery

The host stores an intent before uploading source or attempting a Pod. The Pod
has an evidence finalizer. Cleanup requests normal deletion, observes the
kubelet's container termination, records an immutable termination receipt and
only then removes that finalizer. It never force-deletes a Pod to claim cleanup.
An unscheduled deleting Pod can also be confirmed as never started.

A lost create response retains the intent and source. A replacement host can
stop the recorded Pod without executing the check again. Missing Pod evidence
after an uncertain create remains unknown. A termination receipt permits source
cleanup to resume after a crash following Pod removal. Cleanup uses a bounded
deadline and exact Pod UID fences; a node partition retains evidence and refuses
successful Task finalization. The Task host checks persistent inventory even
when no local validation handler or workspace survived replacement.

## Deployment boundary

`webhook-ingress/infra/codex-validation.tf` provisions the separate validation
namespace, quota, deny policy, source/Pod permissions and a **dedicated** host
service account when explicitly enabled. The default is disabled. The shared
autoscaler and its service accounts receive no Kubernetes permissions from this
change. Existing shell-enabled personas must not inherit this credential.

An explicitly configured dedicated trusted host selects
`ADP_CODEX_VALIDATION_BACKEND=kubernetes` and points
`ADP_CODEX_VALIDATION_KUBERNETES_CONFIG` at a host-owned JSON file containing
`endpoint`, `namespace`, `token_file` and `ca_file`. The SDK receives none of these
values. Task identity comes from the authenticated bootstrap binding. The
executor uses the rotating token file, verifies the CA and refuses redirects;
it does not use ambient kubeconfig, proxy settings or model-supplied URLs.

The production shared worker still needs an authenticated dedicated-host service
and source-transfer composition before it can use this backend. No live ADP
worker rollout or persona registration has been enabled. Follow the canonical
[agent deployment guide](../../adp-platform-deployment/deploy-with-agent.md)
before proposing that rollout. Disabling dispatch is only the first rollback
step: preserve executor credentials and inventory until work is confirmed stopped.
Deleting a namespace or stripping finalizers is not a cleanup receipt.

## Evidence

Local qualification uses kind v0.27.0, Kubernetes v1.32.2 and Calico v3.30.3,
with kubelet `podPidsLimit: 128`. The Calico manifest SHA-256 is
`9382d2b27a76f40c170454b408653e6d71e2205ef0aef069e942bb690e7381d0`.
The immutable check image is
`registry.example/checks@sha256:5c1c5d87388b6885e357c435f1c74703144146aac0d6a0edf3e6fc666f611512`,
loaded into the local container runtime under its digest. No mutable registry
pull selects check code during a validation run.

Seven actual Pod scenarios passed: source/credential/root/capability isolation,
check failure, excessive output, timeout, cancellation, network denial and the
process bound. Unit tests separately exercise lost responses, replacement,
partitioned nodes, scope violations and interrupted cleanup. Twenty Terraform
rollout tests pass, including default-off behavior and dedicated host identity.
These results qualify the local backend, not production Task authentication,
tenant-installed provider authority or the complete foundation story.

The gateway-storage → Task host → official SDK → Kubernetes check → publication
completion scenario also passed with fixture inference/provider authority in
14.23 seconds, including exported OTEL assertions. This uncovered and fixed a
Task identifier mismatch: the executor now accepts the canonical `tsk_` UUIDv4
identity rather than a bare UUID. Model and provider authorities in this scenario
remain fixtures; Kubernetes execution and cleanup are real.
