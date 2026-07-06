# Session Handover — Agent-Runtime gVisor Hardening

**Date:** 2026-07-02
**Effort:** Kernel-level isolation for agent-worker pods on EKS (sub-EPIC [#2358](https://github.com/aws-e/adp/issues/2358), EPIC [#2315](https://github.com/aws-e/adp/issues/2315))
**Status:** Substrate path found + built (PR open, not applied). Next action is a **from-scratch validation in an isolated account** — needs a decision from the user before dispatch.

---

## TL;DR for whoever picks this up

We set out to run untrusted autonomous agent pods under **gVisor** kernel isolation on our EKS cluster. The naive path (install `runsc` on the existing EKS **Auto Mode** nodes) is **conclusively dead** — proven by a hands-on agent investigation, not a guess. The unblocked path is a **second, self-managed Karpenter** provisioning **AL2023** nodes with gVisor baked into `userData`, running *alongside* Auto Mode.

That substrate is **built and in an open PR ([#2558](https://github.com/aws-e/adp/pull/2558), branch `agent/issue-2552`)** but **not merged and not applied**. Before applying it to the live dev cluster we identified a **shared-CRD interference risk** (both Karpenters use the `karpenter.sh` NodePool/NodeClaim CRDs). The user's decision — **validate this from scratch in an isolated AWS account first** (modeled on the [#1320](https://github.com/aws-e/adp/issues/1320) fresh-account deploy runbook) rather than risk the running dev environment. That test issue was **not yet filed** — it's the immediate next step, blocked on two inputs (see "Immediate next action").

---

## Where the work sits (branches & PRs)

### 🔴 OPEN — the live work
| PR / Branch | What | State |
|---|---|---|
| **[PR #2558](https://github.com/aws-e/adp/pull/2558)** — branch **`agent/issue-2552`** | **Self-managed Karpenter + AL2023 EC2NodeClass** — the gVisor substrate. Reviewed, CI green, scope-fenced (no ScaledJob flip). **Not merged, not applied.** | OPEN |

Files in #2558:
- `platform/infra/karpenter-selfmanaged.tf` (404 lines) — standalone Karpenter Helm release (`karpenter 1.1.1`, `oci://public.ecr.aws/karpenter`), controller IAM (IRSA), `KarpenterNodeRole`, instance profile, interruption SQS.
- `platform/infra/ec2nodeclass-gvisor.tf` (164) — `karpenter.k8s.aws/v1` EC2NodeClass, `amiFamily: AL2023`, gVisor `userData` (arch-aware runsc install + containerd runtime registration + restart) **lifted verbatim** from `gvisor-nodegroup.tf`.
- `platform/infra/nodepool-gvisor.tf` (+26/-11) — re-points `nodeClassRef` from Auto Mode `default` → the new EC2NodeClass. Keeps `adp.io/runtime=gvisor` taint/label.
- `platform/infra/main.tf` (+4), `agent_learning/2026-07-01-issue-2552-learnings.md` (115).

### ✅ MERGED — landed this session
| PR | What | Merged |
|---|---|---|
| [#2521](https://github.com/aws-e/adp/pull/2521) | **ops-persona credential self-refresh** — the autonomy unlock (IRSA web-identity refresh recipe). `modules/agent-factory/rules/personas/operations.md`. | 2026-06-30 |
| [#2524](https://github.com/aws-e/adp/pull/2524) | **gVisor Karpenter NodePool** codified (`platform/infra/nodepool-gvisor.tf`). NOTE: as-merged it referenced the Auto Mode `default` NodeClass, which CANNOT run runsc — #2558 fixes this by re-pointing to the EC2NodeClass. #2524 is the scheduling *fence*; #2558 makes it functional. | 2026-06-30 |
| [#2543](https://github.com/aws-e/adp/pull/2543) | **ADP vs. OpenShell positioning doc** (`docs/adp-vs-openshell-positioning.md`). | 2026-06-30 |

### This doc
- Branch **`docs/gvisor-session-handover`** — `docs/gvisor-hardening-session-handover.md` (this file).

---

## Issue map

| Issue | Title (short) | State | Meaning |
|---|---|---|---|
| [#2315](https://github.com/aws-e/adp/issues/2315) | Agent-runtime hardening EPIC | OPEN | Parent |
| [#2358](https://github.com/aws-e/adp/issues/2358) | gVisor sub-EPIC | OPEN | This effort's parent; updated with the resolved substrate path |
| [#2513](https://github.com/aws-e/adp/issues/2513) | gVisor on Bottlerocket (the blocker) | OPEN | **Resolved-as-diagnosis** — Bottlerocket wall proven; impl moved to #2552 |
| [#2552](https://github.com/aws-e/adp/issues/2552) | Self-managed Karpenter + AL2023 | OPEN | **Built → PR #2558** |
| [#2514](https://github.com/aws-e/adp/issues/2514) | gVisor PoC (agent under runsc E2E) | OPEN | Next gate after substrate is applied |
| [#2512](https://github.com/aws-e/adp/issues/2512) | Remove dead `gvisor-nodegroup.tf` | OPEN | Folds in once #2558 lifts its userData |
| [#2376](https://github.com/aws-e/adp/issues/2376) | Flip ScaledJob default → gvisor | OPEN | **Human-gated**, final step |
| [#2540](https://github.com/aws-e/adp/issues/2540) | OpenShell evaluation spike | CLOSED | Verdict: *Different Purpose — stay on gVisor* |
| [#2522](https://github.com/aws-e/adp/issues/2522) | adp-cred 403 for scaledjob SA | OPEN | Side-bug found via smoke test #2518 |

---

## The key technical facts (don't re-derive these)

1. **EKS Auto Mode + Bottlerocket cannot host a custom containerd runtime — ever, from any pod.** #2513 proved this hands-on (~45 min on a live node): `/etc/containerd/config.d/` is tmpfs with SELinux context `etc_secret_t`, writable ONLY by Bottlerocket's `api_t` (thar-be-settings). Every K8s workload — even privileged, hostPID, nsenter — runs as `control_t` and is denied `mounton`/write (audit-log captured). **DaemonSet installer, overlay mount, and `apiclient`/settings-API are all disproven.** Do not revisit them.

2. **The unblocked path (AWS-supported):** self-managed Karpenter alongside Auto Mode. Standard pods → Auto Mode (Bottlerocket, untainted). gVisor agent pods → self-managed Karpenter (AL2023, `runsc` via userData), routed by taint/toleration `adp.io/runtime=gvisor`.

3. **⚠️ The interference risk that made us choose isolated-account testing:** Both controllers share the **`karpenter.sh` CRD group** (`nodepools.karpenter.sh`, `nodeclaims.karpenter.sh` — installed 2026-04-17, owned by Auto Mode). The NodeClass layer is cleanly separated (`eks.amazonaws.com` vs `karpenter.k8s.aws`), BUT NodePool/NodeClaim are NOT. Two unresolved questions before ANY apply to a shared cluster:
   - Does the Karpenter 1.1.1 Helm release **manage/overwrite the `karpenter.sh` CRDs** Auto Mode depends on? (Need `skipCrds`/CRD-skip confirmed — **not verified in #2558**.)
   - Is there **NodePool/NodeClaim ownership scoping** so the two controllers don't dual-reconcile? Auto Mode's embedded Karpenter isn't user-configurable like standalone, so this is the genuinely uncertain part.
   These are exactly why the live cluster should NOT be the test bed.

4. **Autonomy unlock (proven this session):** agents were stalling at ~1h on AWS session expiry. Fix = IRSA / EKS Pod Identity web-identity token auto-refresh (`AWS_ROLE_ARN` + `AWS_WEB_IDENTITY_TOKEN_FILE`, unset the static session). Merged in #2521. The #2513 run then sustained ~59 min+ and the #2552 run completed cleanly — the fix works. `adp-cred` 403s for the scaledjob SA (#2522) — do NOT use it for refresh.

---

## Live cluster snapshot (dev, account 879318057152, profile `embark1`)

- NodePools: `general-purpose` (NodeClass `default`, 1 node, 74d — standard workloads) and `gvisor` (NodeClass `default`, 0 nodes — the #2524 fence; **currently non-functional** because it points at Bottlerocket, fixed by #2558).
- `karpenter.sh` CRDs owned by Auto Mode's embedded controller.
- Nothing from #2558 is applied. Applying `platform/infra/` is **manual-only** via the `platform-infra-apply.yml` workflow (`workflow_dispatch`) — merging a PR does NOT apply it.

---

## Immediate next action (where we stopped)

**File a from-scratch validation issue** modeled on **[#1320](https://github.com/aws-e/adp/issues/1320)** (the fresh-account, phase-by-phase, account-guarded, long-running-orchestrator deploy runbook). Goal: stand up a clean cluster in an **isolated account**, apply the #2558 dual-Karpenter substrate there, and prove it (a) coexists with Auto Mode without CRD interference, and (b) provisions a real AL2023 gVisor node where a `runtimeClassName: gvisor` pod runs and `cat /proc/version` shows `4.19.0-gvisor`, surviving node recycle.

**Blocked on two decisions from the user** (asked, not yet answered):
1. **Target account** — reuse `968027867250` (existing #1320 test account, vault label `adp-test-968027867250`, verified assumable role; may carry leftover state) **or** a brand-new account (user supplies account ID / vault label / user-id / assumable role).
2. **Scope depth** — platform infra only (Phases 1–3 of #1320: bootstrap + preflight + `platform-infra-apply` → VPC + EKS Auto Mode; enough to prove the substrate) **or** full ADP deploy + a live agent under `runtimeClassName: gvisor` end-to-end.

Once those are answered: file the issue (five-section convention; account-guard hard rule; per-phase in-account validation; child-issue protocol for every manual fix; the #2558 apply + dual-Karpenter validation as the final phase), verify the body has **zero stray `@agent-` tokens**, then dispatch `@agent-operations` via a comment mention.

---

## Operating conventions used this session (keep these)

- **Trigger agents via comment mention** (`@agent-operations` / `@agent-architect`), NOT labels. Issue *bodies* must contain **zero `@agent-` tokens** (the mention parser routes to the first one it sees) — verify with `grep -oE '@agent-[a-z]+'` before dispatch.
- **AWS profile `embark1`** (account 879318057152) for the live dev cluster. Never hardcode/echo/log credentials.
- **Dispatch briefs** carry: session-extension recipe (IRSA refresh), a scope fence, convergence guardrails (single hypothesis, 2-attempts-then-status, no architecture pivots), and a HARD validation gate (`kubectl --dry-run=server` for heredoc YAML — terraform plan does NOT validate it).
- **The flip (#2376) requires explicit human confirmation** — never auto-dispatch it. Revert is sub-minute (one `runtimeClassName` field).
- `platform/infra/` is **manual-apply only**; a second controller on a shared cluster is a weighty action — apply only with the user watching.

## References
- Blocker root-cause: [#2513 findings](https://github.com/aws-e/adp/issues/2513)
- Substrate PR: [#2558](https://github.com/aws-e/adp/pull/2558) (branch `agent/issue-2552`)
- Fresh-account runbook template: [#1320](https://github.com/aws-e/adp/issues/1320)
- OpenShell positioning: `docs/adp-vs-openshell-positioning.md` ([#2540](https://github.com/aws-e/adp/issues/2540))
- Deploy procedure: `docs/adp-platform-deployment/deploy-quickstart.md`
