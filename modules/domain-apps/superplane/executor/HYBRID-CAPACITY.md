# Upstream Superplane scenario audit

Read-only source: `aws-innovate/AISuperPlane`, SHA `5d543c952493f0765133b92e93301b0b24d028ee`. Maintained implementation remains `aws-e/adp`. Issue titles/closure do not substitute for recorded outcomes. These are historical observations, not newly verified live state.

## Recovered evidence

| Scenario | Recorded result | Evidence limit |
|---|---|---|
| [#44 Nebius H100 joins EKS](https://github.com/aws-innovate/AISuperPlane/issues/44#issuecomment-4142685265) | Ready H100, GPU visible, nvidia-smi and logs/exec through WireGuard. [Reverification](https://github.com/aws-innovate/AISuperPlane/issues/44#issuecomment-4144756207) lists native HyperPod plus Nebius L40S/H100. | Native HyperPod was ml.t3.medium in #39; no proof of AWS GPU execution. Initial port-forward skipped. |
| [#46 Qwen on Nebius](https://github.com/aws-innovate/AISuperPlane/issues/46#issuecomment-4143198996) | Qwen3-Coder-30B-A3B-Instruct-FP8 through vLLM; model listing, chat completion and tool calling recorded successful. | One external serving node, not shared AWS+Nebius serving. |
| [#57 NCCL](https://github.com/aws-innovate/AISuperPlane/issues/57#issuecomment-4145430763) | Two Nebius H100 nodes joined EKS; both all_reduce ranks passed; PyTorchJob Succeeded. | Same Nebius VPC, not cross-provider training. Issue open. Broad WireGuard route broke local traffic; Cilium/runtime/DNS required fixes. |
| [#212 remote AWS GPUs](https://github.com/aws-innovate/AISuperPlane/issues/212#issuecomment-4187107972) | H100s in ap-south-1 and ap-northeast-1 joined us-east-1 EKS. | [50-user load result](https://github.com/aws-innovate/AISuperPlane/issues/212#issuecomment-4187182844): 2,100 requests, 100% success, 674.6 tokens/s explicitly measures Node 1, not aggregate balancing. |
| [#324 Gemma shared serving](https://github.com/aws-innovate/AISuperPlane/issues/324#issuecomment-4230186203) | Four GPU replicas; 100 concurrent users, 38.3 RPS, 4,574 tokens/s; private ALB/cross-cluster access described. | All four GPU nodes actually landed in ap-northeast-1. Multi-region title exceeds recorded result; no mixed-provider serving evidence. Issue open. |
| [#39 observability](https://github.com/aws-innovate/AISuperPlane/issues/39#issuecomment-4141238404) | Nebius GPU workload and DCGM exporter worked. | External OTel collector required EC2 IMDS; AMP/Grafana failed or remained unknown. |
| [#48 autoscaler](https://github.com/aws-innovate/AISuperPlane/issues/48), [#51 cloud adapters](https://github.com/aws-innovate/AISuperPlane/issues/51) | Specify pending-pod capacity, price selection/fallback, idle consolidation/recovery. | Requirements need mapping to governed runtime and separate demonstration. |
| [#311 HyperPod](https://github.com/aws-innovate/AISuperPlane/issues/311), [#317 mixed workspace](https://github.com/aws-innovate/AISuperPlane/issues/317) | Explicit native HyperPod AWS + neocloud nodes on one EKS cluster. | No comments proving newer product integration complete at this audit. |
| [#232 multi-replica serving](https://github.com/aws-innovate/AISuperPlane/issues/232) | Intended 4–6 replicas / 100 users; comments show early network work. | Insufficient completion evidence. |

Source read: `research/eks-hybrid-gpu-serving-blog.md`, `research/gemma4-31b-load-test.md`, `infra/hybrid-node-prereqs/README.md`, `poc/eks-hybrid-skypilot/onboard-node.sh`. The blog's blanket claim that remoteNetworkConfig cannot be retrofitted conflicts with #39's successful update. Verify actual current prerequisites.

Historical scripts contain mutable images, task-environment activation material, validation bypasses and hostNetwork assumptions. Preserve topology/lessons through maintained approval, credential, inventory and recovery boundaries. Do not execute scripts or follow old instructions to launch/retain resources.

## Maintained-path gaps and order of work

Domain paths are relative to `modules/domain-apps/superplane`; the shared Harness path is relative to the repository root. Audited ADP main `70e0d36c` (workload implementation tested at `2948b353`).

| Boundary | Current code | Required extension |
|---|---|---|
| Plan/approval | `executor/superplane_executor/plan.py`, `deployment_plan.py`: AWS only; EKS region/account equals compute; AMI/profile required. | Separate cluster and capacity identities; versioned native/hybrid plans binding network/join, exact provider credentials, images and finite envelopes. Preserve old requests. |
| Credentials/backend | `provider.py` and SkyPilot guard: AWS role/web identity and EC2 tagging. | Restricted Nebius credential delivery/attestation. No worker-selected credentials or activation secrets in task state. |
| Network/join | `provider.py`: same VPC, EC2_LINUX, native nodeadm. | Governed hybrid bootstrap with correct HYBRID_LINUX prerequisites, scoped tunnel/private routes, CNI/GPU readiness and independently owned shared-network cleanup. |
| Inventory/recovery | EC2 lookup, AWS vocabulary, original Kubernetes UIDs. | Nebius instance/disk/network and join identity; durable ownership before further effects; provider recovery and confirmed absence. Unknown replies retain exposure. |
| Shared execution effects | `modules/harness/jobs/harness_jobs/effects.py` recognizes AWS status/removal but has no Nebius entries. | Add exact provider/action semantics with matching provider-hook tests and durable SQL epoch-trigger parity; do not treat an unknown action as an observational read. |
| Workload placement | Allocation-scoped taint/selector and registry. | Both providers' allocations in one cluster, explicit job placement and provider-backed evidence. HyperPod needs its own adoption/lifecycle path. |
| Serving | One replica/profile and Service per allocation. | Approved serving group spanning allocations, exclusive selectors, shared endpoint, backend correlation, bounded authenticated access and member removal. |
| Observability/cost | Workload observations/results and recorded-rate estimates. | Both providers' telemetry without IMDS assumptions, cleanup exposure and separate billing evidence. |

Existing Nebius/Lambda Go adapters are unreachable through the governed path: the executor deliberately refuses legacy-provider fallback. Removing an AWS guard alone bypasses missing identity/ownership/cleanup composition.

Prioritize multi-provider capacity/network/join lifecycle, then shared serving and authenticated use. Keep completed workload/result/cost fixes.

The [revised demo](../tests/acceptance/MIXED-PROVIDER-DEMO.md) is the acceptance baseline. CI checks code; a separately authorized live evaluation establishes actual mixed-provider behavior. Historical evidence is linked directly above; upstream source paths refer to the pinned revision.
