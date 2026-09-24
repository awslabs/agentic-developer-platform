# Superplane working-system demonstration

Acceptance baseline: AWS and neocloud GPU capacity in **one EKS cluster**, with real workloads on both. The CUDA job is a component test. This document specifies the required demonstration; its production composition and executable driver are still required. It records no completed live run and grants no spending authority.

## Exact main demo

Use one ADP organization/workspace and one EKS data-plane cluster, separate from management. Select and reverify its canonical UUIDs, ARN, CA, endpoint, account and namespace from the current installation record. Historical upstream clusters/accounts are evidence, not live targets.

Minimum capacity: **two GPUs simultaneously, one AWS and one Nebius**. A native CPU-only HyperPod node does not meet the AWS GPU requirement. Native EKS capacity can prove AWS execution; claiming HyperPod support needs the additional qualification below. Freeze provider regions/types, image/model digests, credentials references, network profiles, finite deadlines, per-provider reservations and aggregate spend ceiling in the reviewed run configuration. Provider unavailability blocks this demo rather than silently making it AWS-only.

| Step | Action | Required pass evidence |
|---|---|---|
| 1 | Authenticate in ADP and select the workspace. | Installed source/image/schema/cluster bindings agree; baseline ADP health and inventory recorded. |
| 2 | Review/approve AWS and Nebius capacity and acquire both through ADP. | One durable allocation per approved request; finite bounds and exact identities. Reload/retry preserves original operations. Credentials and activation secrets do not enter browser/task history. |
| 3 | Observe both GPU nodes in the same EKS cluster at the same time. | Ready and allocatable GPUs; provider API identities map each original node UID to its actual instance/allocation. Labels alone are insufficient. |
| 4 | Verify hybrid networking and node runtime. | EKS-to-kubelet logs/status, DNS and private service traffic across the two pools work; CNI/NVIDIA runtime/device plugin healthy. Routes preserve provider-local traffic; no validation bypass or tenant hostNetwork exception assumed. |
| 5 | Submit two pinned CUDA Jobs, one constrained to each allocation. | Both Jobs succeed on the intended provider. Each original Pod/node/allocation/provider identity is recorded and checksum is 33,554,432. |
| 6 | Read logs and retained results through ADP; repeat submission/reload. | Original identities/results agree; no duplicate execution. Results remain readable after original Jobs are removed. |
| 7 | Serve one pinned model with a replica on each provider behind **one private Service and authenticated ADP endpoint**. | Both original Pod UIDs are ready EndpointSlice members and map to the two verified allocations. The Service selects only the approved serving group. Two unrelated endpoints do not pass. |
| 8 | Send 40 bounded inference requests to that endpoint at concurrency 4, fixed prompt, maximum 128 output tokens. | All 40 succeed with approved model identity/nonempty output. Trusted request/backend correlation proves at least one completed request on each provider. Endpoint membership alone is insufficient. Record latency/tokens/failures. |
| 9 | Gracefully remove one serving member through the approved path; send 10 requests. | Same endpoint continues serving from the remaining provider; all 10 succeed. Lost responses remain unknown rather than causing automatic re-execution. |
| 10 | Test unauthenticated, wrong-workspace and revoked access; switch workspace during a request. | Access refused without model output/credential leakage; late output from old scope discarded. |
| 11 | Stop workloads and release demo-owned capacity/network/join resources through ADP. | Fresh provider reads confirm original owned compute/storage/network absence on both providers; original Kubernetes UIDs absent. Baseline/shared infrastructure preserved by recorded ownership. Uncertain deletion retains inventory and reservations. |
| 12 | Compare final inventory, costs and ADP health. | No unexplained resources; retained resources have owner/deadline/exposure. Reservations, estimates and provider bills remain separate. ADP healthy. Billing-dependent acceptance pending until actual provider billing evidence arrives. |

Missing required evidence is BLOCKED or FAIL, never an implicit pass. CI fixture results do not certify a live run.

## Workload contract

A bundled script in a digest-pinned PyTorch CUDA image creates two 256×256 float32 CUDA tensors filled with 1 and 2, multiplies and synchronizes them, and verifies 65,536 entries equal 512 and total 33,554,432. It writes a version 1 result below 4,096 UTF-8 bytes to `/dev/termination-log`. Run the same bounded script on each provider without runtime source/dataset downloads.

Use the same immutable serving image, model repository revision and tokenizer artifacts on both providers. The installed image contract must support authenticated health/inference, bounded text requests and request/backend correlation without secret exposure. Record model identity separately from generated text. Do not assert an exact generated phrase or imply the CUDA job trained this model.

## Additional required scenario qualifications

| Scenario | Qualification |
|---|---|
| AWS remote-region capacity (#212) | Nodes in two distinct AWS regions join one EKS cluster; actual routes/TGW/peering observed and jobs run on both. A shared-serving claim also requires correlated requests to both regions. |
| Four-replica serving (#324) | Four GPU replicas, one endpoint, progressive concurrency 10/25/50/100 within separately approved bounds. Record actual regions/providers and throughput; historical 4,574 tokens/s is not inherited. |
| Distributed training (#57) | Two Nebius GPUs in a suitable same-provider network run pinned two-rank NCCL all_reduce successfully. Cross-provider training remains a distinct, unproven scenario. |
| HyperPod mixed mode (#311/#317) | Actual AWS HyperPod GPU instances plus Nebius GPUs coexist and execute work on one EKS cluster. Native EC2 alone does not prove HyperPod lifecycle support. |
| Pending-demand/fallback (#48/#51) | Select cheapest permitted compatible capacity; definitive pre-launch failure can fall back within approval. Ambiguous launch recovers its original allocation first. Idle consolidation/health recovery preserve ownership/accounting. |
| Observability (#39) | Fresh GPU metrics from both providers reach the product with verified node identity, without assuming external-node EC2 IMDS. Exporter readiness alone is insufficient. |

Retain #5540's new-account bootstrap, BYOC preservation, installed upgrade/resume/rollback, cancellation, controller restart, lost-provider-acknowledgement and feature-off qualifications. They remain necessary for the corresponding platform claims.

## Evidence and present gaps

Driver output must include per-step outcomes, timestamps/actions, source/image/model/schema/cluster identity, original request/operation/allocation/provider/Kubernetes IDs, serving request/backend correlation, before/after inventory, reservations/estimates/billing evidence, cleanup ownership and evidence hashes. Bind observations to one run/cluster/time window. Exclude raw credentials, activation material and private prompts.

The current governed executor accepts only AWS native nodes in the EKS region/account/VPC. Credential delivery, backend guard, identity, inventory and cleanup are AWS-specific. Serving supports one replica and per-allocation Service selectors. Implement governed hybrid networking/join/provider lifecycle, shared serving/authenticated inference and the driver before calling this runnable. See [upstream scenario audit](../../executor/HYBRID-CAPACITY.md).
