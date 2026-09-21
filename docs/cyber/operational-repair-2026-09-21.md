# Embark 1 cyber worker repair — 21 September 2026

The cyber worker infrastructure is operational in account `879318057152`,
region `us-east-1`, cluster `adp-dev-cyber-eks`. The failed conventional node
group has been removed; the cluster now uses EKS Auto Mode capacity.

## Diagnosis and changes

- The Auto Mode node role had an `EC2_LINUX` access entry without the Auto Node
  policy. Restored the `EC2` entry with `AmazonEKSAutoNodePolicy`. This entry is
  owned by EKS for the built-in node pool; the role must not be reused for a
  conventional managed node group.
- Added `sts:TagSession` to the EKS cluster role trust for Auto Mode.
- Changed KEDA TriggerAuthentication from `aws-eks` to `aws`, using the
  operator credential chain. The previous configuration attempted an
  unauthorized role chain through the worker role.
- Added worker Pod Identity trust restricted to this cluster, namespace
  `cyber-workers`, and service account `cyber-worker`. Imported the existing
  Pod Identity association into the cyber Terraform state.
- Enabled the network-policy controller. Its previous configuration was
  `enable-network-policy-controller: "false"`, so the worker isolation
  policies were not enforced.
- Added a private STS endpoint in both private subnets and a worker egress
  policy permitting regional S3/DynamoDB gateway-prefix traffic on TCP 443
  and the node-local Pod Identity endpoint on TCP 80. Both new resources were
  imported into the cyber Terraform state.
- Scheduled the workers on Auto Mode and moved KEDA and DNS onto healthy
  Auto Mode capacity. Retired `cyber-workers-ng` and its old EC2 instance
  after replacement capacity and successful analysis results were verified.

The corresponding Terraform and Kubernetes fixes are saved in
`modules/domain-apps/cyber/`. Existing unrelated work in the main checkout was
preserved. The repair is recorded on branch
`fix/cyber-auto-mode-repair-20260921`.

## Verification

The final harmless sample was a Python file that prints a verification message:

- Artifact: `cyber-repair-final-20260921T213655Z`
- SHA-256: `018fa0c2de0ae38b40e9695a5cef464d86a60cbb77de1c178e9ef1a78e551272`
- Triage job `cyber-triage-scaledjob-brjmc`: completed in 20 seconds, zero
  failed attempts; persisted hash matched the uploaded sample.
- Static job `cyber-static-scaledjob-984pz`: completed in 16 seconds, zero
  failed attempts; persisted status `ok`, with no YARA hits. Static workers
  successfully fetched the 734-file public rules bundle.
- Both results were read back consistently from DynamoDB. Both task queues
  and both dead-letter queues were empty after completion.
- Network verification from an isolated Auto Mode worker allowed SQS and STS
  and blocked a TCP 443 connection to `example.com`.
- Auto Mode node, `general-purpose` NodePool, and `default` NodeClass were
  Ready. The managed-node-group list was empty. KEDA and CoreDNS were ready.
- The CAPE load-balancer target remained healthy.
- Terraform formatting and validation passed with zero errors; four existing
  provider warnings concerned the existing CAPE user-data encoding and
  deprecated DynamoDB arguments. Rendered Kubernetes manifests passed server
  dry-run validation and were applied.

Temporary admin access used for the repair was removed and its removal was
verified. Failed diagnostic jobs and the temporary capacity probe were cleaned
up; successful verification jobs and sample/result artifacts remain available.

Machine-readable evidence is in
[`operational-repair-2026-09-21.json`](operational-repair-2026-09-21.json).
This verification covers worker provisioning, queue dispatch, triage, static
analysis, storage, and the tested network paths. It did not exercise a complete
seven-stage agent conversation, dynamic guest detonation, or URL analysis.
