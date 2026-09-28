# Standalone validation isolation

This module provisions a dedicated EKS Auto Mode NodeClass and bounded NodePool,
plus the validation namespace, deny policy, quota and service RBAC targets. It
does not update worker admission, IAM, the gateway, or the webhook stack. Keep
`codex_kubernetes_validation_enabled=false` in webhook Terraform when this module
owns these names. Never enable both owners or import resources without reviewing
their current state.

Use the canonical deployment guide and a reviewed saved plan for the approved
account. The Auto Mode CRD must support
`advancedCompute.kubelet.podPidsLimit`; validate against the actual cluster schema.
The node role requires the existing Auto Mode access entry. Supply private
subnets and the reviewed node security group from that cluster.

The pool selects one m6a.xlarge instance type with a four-vCPU pool limit. Nodes
are tainted `adp.dev/validation=only:NoSchedule`, have a PID limit of 128, and
start with strict network denial. No node gets the executor's
`adp.dev/validation-isolation=v1` label from this module. Its candidate label is
only for operator probes. Auto Mode limits are eventually consistent; verify
actual node count and costs during qualification.

Run `../qualify-isolation.py --image <immutable Python Lambda image> --evidence
<path>` through the private deployment identity. It checks a reachable network
control on the same node, ingress/egress denial, metadata denial, process limits,
no workload credentials, seccomp/capabilities and read-only root. It records
observed termination before removing evidence finalizers. Interrupted or unknown
cleanup requires reconciliation; never force-delete to obtain a passing result.

Only qualify the exact observed node UID after inspecting a passing receipt and
confirming that its NodeClass configuration has not changed. A replacement node
does not inherit qualification. The pool expires nodes after 24 hours; readiness
must not imply indefinite qualification. Preserve execution/cleanup evidence and
requalify replacements before scheduling real validation. The service's existing
source/check/timeout/cancellation qualification remains a separate requirement.

The operator's `--promote` option labels the exact observed node UID only after
all probes and cleanup pass. It also marks that dedicated node
`karpenter.sh/do-not-disrupt=true` to retain it during the bounded rollout. Remove
this annotation after stopping validation admission and confirming all executions
terminated; never retain it as an unattended permanent capacity setting.

Teardown requires validation admission stopped and every execution confirmed
terminated. Remove the standalone service and its bindings before destroying
these resources. An empty namespace or absent Pod alone is not termination proof.
