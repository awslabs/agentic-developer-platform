# ADP vs. NVIDIA OpenShell — Positioning & Defense

**Status:** Decided — *stay the course on gVisor*
**Date:** 2026-06-30
**Source spike:** [#2540](https://github.com/aws-e/adp/issues/2540) — architect design note: [issue comment](https://github.com/aws-e/adp/issues/2540#issuecomment-4847863077)
**Architect run:** https://github.com/aws-e/adp/runs/84395133836
**Related:** gVisor sub-EPIC [#2358](https://github.com/aws-e/adp/issues/2358), core blocker [#2513](https://github.com/aws-e/adp/issues/2513), hardening EPIC [#2315](https://github.com/aws-e/adp/issues/2315)

---

## Why this doc exists

"Should we use NVIDIA OpenShell instead of (or alongside) our gVisor-based agent
isolation?" is a question that recurs with every new stakeholder. This doc is the
citable answer. It captures the verdict, the defense of ADP's architecture *for
the job ADP does*, and the honest caveats — so the comparison doesn't have to be
re-litigated from scratch each time.

**TL;DR:** OpenShell is an excellent **single-developer** agent sandbox runtime.
ADP is a **multi-tenant, server-side, event-driven agent fleet** with kernel-level
isolation, ephemeral-by-design pods, and native AWS identity. They are built for
**different missions**. For ADP's mission, adopting OpenShell would mean *unwinding*
our security hardening to gain capabilities we already exceed. We stay the course
on gVisor and monitor the `agents.x-k8s.io` Kubernetes SIG standard rather than the
NVIDIA product.

---

## How this decision was reached (the run)

This was not a desk opinion — it was produced by an **autonomous architect-agent
research spike**, dispatched onto issue [#2540](https://github.com/aws-e/adp/issues/2540):

- The architect agent was asked to evaluate [NVIDIA/OpenShell](https://github.com/NVIDIA/OpenShell)
  against ADP's in-flight gVisor isolation work and return a single verdict —
  **complementary / overlapping / different-purpose** — with rationale.
- It read OpenShell's repo (README, the **`crates/openshell-driver-kubernetes`**
  crate-level docs, Helm chart), the official docs site, and cross-referenced ADP's
  live code (`nodepool-gvisor.tf`, `gvisor-runtime.tf`, `scaledjob.tf`,
  `scaledjob-netpol.tf`) plus prior agent learnings from [#2511](https://github.com/aws-e/adp/issues/2511)/[#2374](https://github.com/aws-e/adp/issues/2374).
- It delivered a 12-dimension capability comparison, an architecture-fit analysis
  (EKS Auto Mode, KEDA, IRSA, inference routing), a reuse table, and a maturity/risk
  assessment.
- **Verdict: Different Purpose — continue [#2513](https://github.com/aws-e/adp/issues/2513)
  as-is.** No adoption, no PoC, no follow-up issues under [#2358](https://github.com/aws-e/adp/issues/2358).

The full design note is the authoritative artifact:
[#2540 design note](https://github.com/aws-e/adp/issues/2540#issuecomment-4847863077).
Run: https://github.com/aws-e/adp/runs/84395133836.

> Note: this spike itself ran on the ADP agent fleet it was evaluating — a
> multi-tenant, webhook-triggered, ephemeral agent pod doing autonomous research.
> That is the deployment model the rest of this doc defends.

---

## The frame: defend on mission-fit, not feature count

A defense that claims ADP is "better than OpenShell" in the abstract is weak and
easy to puncture — OpenShell is genuinely better at the job *it* targets. The
correct, airtight framing is **mission-fit**:

| | ADP | OpenShell |
|---|---|---|
| **Mission** | Multi-tenant, server-side agent **fleet** | Single-developer **workstation** runtime |
| **Trigger** | Webhook event → SQS → KEDA ScaledJob | `openshell sandbox create` (interactive) |
| **Pod lifecycle** | Ephemeral: create → run → **destroy** | Persistent: PVC-backed workspace |
| **Concurrency** | N tenants × M concurrent pods | One developer, one gateway |
| **Maturity** | gVisor: 8 yrs, prod in GKE | v0.0.73 alpha, ~90 days old |

Every ADP design choice flows from "multi-tenant ephemeral fleet." On ADP's own
axes, our architecture wins decisively.

---

## The five defensible pillars

### 1. Multi-tenancy — OpenShell concedes this outright
OpenShell self-describes as *"single-player mode — one developer, one environment,
one gateway."* ADP runs per-tenant SQS `MessageGroupId` isolation, IRSA-scoped
roles, vault isolation, and a gateway with per-tenant budgets / metering / audit.
This is not "we're ahead" — it's a capability OpenShell **does not have and is not
yet trying to build**. For ADP's mission, it is the whole ballgame.

### 2. Ephemeral > persistent for our threat model
OpenShell uses PVC-backed **persistent** workspaces. ADP **destroys** the workspace
at pod termination (`restartPolicy: Never`, no PVC). For untrusted autonomous
agents, statelessness is a **security feature**: no cross-run contamination, no
data-at-rest, nothing to exfiltrate from a prior session, no EBS lifecycle to
manage. Their persistence is a liability *for our use case*, not something to envy.

### 3. Kernel-layer isolation is strictly stronger than app-layer
This is the one place ADP is **unambiguously** more defensible on raw security:
- **gVisor** — the `runsc` Sentry reimplements 300+ syscalls in userspace; the
  agent **never touches the host kernel**. A kernel 0-day is intercepted by
  construction.
- **OpenShell** — Landlock LSM + a seccomp **denylist** (~30 blocked syscalls) on
  the **standard shared host kernel**. A 0-day in an un-denylisted syscall escapes.

For "run untrusted agent code," gVisor is the correct primitive.

### 4. Native credential + inference model
- **Credentials:** IRSA / Pod Identity issues short-lived, role-scoped AWS creds
  via STS — no env-var secrets — purpose-built for our Bedrock / SQS / DynamoDB
  needs. OpenShell's "providers" inject developer API keys (`ANTHROPIC_API_KEY`,
  etc.) as env vars and have **no concept of AWS role assumption**.
- **Inference:** our gateway Bedrock proxy is a **superset** of OpenShell's privacy
  router — credential-hiding **plus** multi-tenant billing, budgets, rate-limiting,
  audit, and an admin UI. We do everything they do at the egress boundary, plus the
  operations layer they lack. The overlap is surface-level only.

### 5. Maturity & dependency risk
gVisor: 8 years, production in GKE, K8s-native RuntimeClass (GA). OpenShell:
**v0.0.73 alpha, ~90 days old, "expect breaking changes," K8s path "experimental."**
Betting a *security boundary* on alpha software (with corporate-priority risk) is
the indefensible move. Staying first-party here is the conservative, correct call.

---

## Would OpenShell even *fit* in ADP? (the embedding question)

Asked directly: **can OpenShell be packaged into ADP worker pods?** No — and not for
lack of effort. OpenShell's K8s driver requires capabilities and defaults that
**directly contradict ADP's hardened pod posture**:

| OpenShell K8s-driver requirement | Conflict with ADP |
|---|---|
| `CAP_NET_ADMIN` for per-sandbox network-namespace setup | ADP drops **ALL** capabilities ([#2363](https://github.com/aws-e/adp/issues/2363)) |
| AppArmor **Unconfined** default for sandbox pods | ADP uses seccomp `RuntimeDefault` |
| `agents.x-k8s.io` Sandbox CRD + controller | New uninstalled control-plane surface on EKS Auto Mode |
| PVC-backed persistent workspace | ADP is ephemeral by design (no PVC) |
| Gateway-managed interactive lifecycle | Incompatible with KEDA one-shot ScaledJob; would require rewriting webhook-ingress |

Embedding OpenShell would mean **unwinding the hardening we are adding** to gain
capabilities we already exceed. It also does **not** shortcut the [#2513](https://github.com/aws-e/adp/issues/2513)
blocker — it sidesteps `runsc` only to hit its own equivalent wall (CRD controller,
NET_ADMIN, AppArmor) on EKS Auto Mode.

---

## Intellectually honest caveats (what OpenShell does better)

A defense that pretends OpenShell has no edge is weak. Concede cleanly:

- **Developer experience for solo devs** — `openshell sandbox create -- claude`,
  hot-reloadable YAML policy, a k9s-style TUI. We don't have that polish; for their
  target user it's compelling. Irrelevant to our fleet, but real.
- **L7 binary-identity egress** — knowing *which binary* made *which request* is a
  genuine forensics capability we lack. We judged it not worth the `NET_ADMIN` cost,
  but it's a real feature, not vaporware.
- **#2513 is still open on our side** — we cannot yet claim "fully shipped kernel
  isolation." We *can* claim the architecture is proven (NodePool live, RuntimeClass
  deployed, `runsc` runs) with one operational binary-delivery problem remaining.
  OpenShell would not shortcut that problem.

---

## What we *are* taking from the analysis

| Item | Decision |
|---|---|
| Adopt OpenShell as a replacement | ❌ No — different mission |
| Adopt OpenShell as a complement | ❌ No — architecturally incompatible lifecycle |
| `agents.x-k8s.io` CRD (Kubernetes **SIG**, not NVIDIA-specific) | 🟡 **Monitor** — potential future standard for agent lifecycle; watch for GA |
| Declarative YAML policy model | 🟡 Maybe long-term — marginal value over our existing NetworkPolicy + securityContext + IAM |
| "Never write credentials to filesystem" principle | ✅ Already done — IRSA projected tokens, memory-only vault secrets |
| DaemonSet-based `runsc` installer for [#2513](https://github.com/aws-e/adp/issues/2513) | ✅ Lead path — this is how GKE Sandbox installs gVisor internally |

---

## The one-line version

> *OpenShell is an excellent single-developer agent sandbox. ADP is a multi-tenant
> server-side agent fleet with kernel-level isolation, ephemeral-by-design pods, and
> native AWS identity. They're built for different missions — and for ours, OpenShell
> would require unwinding our security hardening (NET_ADMIN, AppArmor Unconfined,
> persistent PVCs) to gain capabilities we already exceed. We stay the course, and we
> monitor the `agents.x-k8s.io` SIG standard rather than the NVIDIA product.*
