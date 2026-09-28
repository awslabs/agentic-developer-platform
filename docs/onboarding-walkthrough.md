# ADP Operator Onboarding Walkthrough

**Audience:** the person who just got ADP deployed (or is about to) and now has to make a
team productive on it. Day 1 → week 1.

**What this is:** the canonical path through setup, your first agent run, governance, and
the failure modes you will actually hit. It is a **map, not a manual** — every step links
to the authoritative doc for that step rather than restating it, because restated commands
drift and then you follow the wrong ones. If a command appears here and also in a linked
doc, **the linked doc wins.**

> **Before you start:** know which deploy track you are on. Everything below forks on it,
> and the two tracks are genuinely different systems — see [Step 1](#step-1--setup-pick-your-track-first).

---

## The path at a glance

| Step | You do this | Time | You know it worked when |
|---|---|---|---|
| [1](#step-1--setup-pick-your-track-first) | Get the platform deployed (one of two tracks) | hours | Dashboard loads, `/api/health` healthy |
| [2](#step-2--become-the-first-admin) | Seed the first admin | ~5 min | `/access/status` → `registered` |
| [3](#step-3--connect-github) | Connect GitHub (App register + install) | ~15 min | Connections shows your installation |
| [4](#step-4--your-first-agent-run) | Trigger your first agent | ~5 min | A PR appears, authored by the agent |
| [5](#step-5--know-your-agents) | Learn the persona catalogue | reading | You know which mention to type |
| [6](#step-6--governance) | Roles, access approvals, budgets | ~30 min | Teammates can log in; budgets set |
| [7](#step-7--troubleshooting-the-things-that-actually-break) | Read the failure modes *before* hitting them | reading | — |
| [8](#step-8--best-practices) | Adopt the issue + review conventions | ongoing | Agents land mergeable PRs |

---

## Step 1 — Setup: pick your track first

Use the self-managed path for customer deployments. The former cross-account
ADP-managed track is unavailable because linked roles do not authorize platform
bootstrap.

### Track A — Self-managed (you run it, in your own AWS account)

You run `terraform`, `aws`, and `kubectl` yourself from your terminal.

→ **[`docs/adp-platform-deployment/deploy-quickstart.md`](adp-platform-deployment/deploy-quickstart.md)**
is the authoritative, verified, phase-by-phase procedure, maintained against real
end-to-end runs. **Follow it, not a summary of it.**

Two things about that doc that save you an hour:

- **`deploy-all.sh` does not finish the job.** It chains Phases 1–6 (including the 6b ALB
  pass) but **not** 6c (broker Lambda code), 6d (first admin), 7 (webhook agent stack), or
  9 (GitHub App). Terraform intentionally ships *placeholders* for artifacts a
  push-triggered CI workflow normally publishes, and a fresh manual deploy fires none of
  those. Skipping the follow-up scripts leaves you with no working login and no agent path.
- **Scope it down if you can.** `--gateway-only` needs no GitHub at all and is the fastest
  way to a working dashboard. Add the agent path afterwards.

Longer reference: [`self-managed-deploy.md`](adp-platform-deployment/self-managed-deploy.md).
Resource → validation-command mapping: [`deployment-manifest.md`](adp-platform-deployment/deployment-manifest.md).

If you would rather have an AI agent drive the deploy, that is a supported path:
[`deploy-with-agent.md`](adp-platform-deployment/deploy-with-agent.md).

### Track B — ADP-managed cross-account (unavailable)

Do not use a linked AWS role or `customer_account` configuration to deploy ADP.
Dashboard-linked roles support steady-state personal account inspection or
shared Bedrock routing only. They cannot create the platform, and adding
administrator access would defeat their trust and revocation contract.

The status and requirements to re-enable this track are documented in
[`docs/adp-platform-deployment/adp-managed-deploy.md`](adp-platform-deployment/adp-managed-deploy.md).
Until those requirements ship, use Track A, optionally with an agent following
the canonical agent deployment guide.

---

## Step 2 — Become the first admin

A fresh deploy has **zero rows in the `users` table**. The onboarding gate therefore
answers "request access" for *everyone* — including the admin user your deploy seeded in
Cognito — and there is nobody with authority to approve anyone. This is a chicken-and-egg
lockout, and it is expected until you break it.

Break it with the bootstrap script (Phase 6d in the quickstart):

```bash
modules/gateway/scripts/bootstrap-admin.sh --env dev
```

Use `--email` / `--pool-id` / `--org` if you are promoting your own SSO admin rather than
the seeded test admin. The script is idempotent.

**Two writes, not one — and this is the part people get wrong.** Being an admin requires
both a **database row** *and* **Cognito user attributes**:

- The DB row + Cognito `admins` group give you backend authority (`/access/status`,
  approvals).
- The frontend's notion of your role and org comes from the access token's `custom:role`
  and `custom:org_id` claims, which a pre-token-generation Lambda copies from your
  **Cognito user attributes** — it never reads the database.

Seed only the DB row and you get an admin who is "registered" but whose UI shows an empty
role, with platform-admin views (including the GitHub App setup CTA) hidden. The script
does both writes.

**→ Log out and log back in afterward** so a fresh token carries the claims.

Verify both halves — the [quickstart's Phase 6d](adp-platform-deployment/deploy-quickstart.md)
has the exact `curl` and `aws cognito-idp admin-get-user` commands. Expect
`{"status":"registered"}` and `custom:role=platform_admin`.

---

## Step 3 — Connect GitHub

**There is no upfront GitHub setup in ADP.** GitHub gets wired at the *end* of a deploy,
not the beginning. If you are gateway-only, skip this step entirely.

### The primary path: the Connections UI

As a `platform_admin`: **Dashboard → Settings → Connections → "Set up GitHub App"**
(route: `/settings/connections`).

This drives GitHub's App-manifest flow: ADP hands GitHub a pre-filled App definition, you
approve it, and ADP stores the App ID, private key, webhook secret, and OAuth client
credentials in the right places automatically. This is the path to prefer — it cannot
misconfigure the webhook URL or forget a permission, because it does not ask you to type
them.

> If the "Set up GitHub App" CTA is not visible, you are not being seen as a
> `platform_admin` — go back to [Step 2](#step-2--become-the-first-admin) and check your
> Cognito attributes. This is by far the most common cause.

### The CLI fallback (headless / no browser session)

```bash
modules/agent-factory/webhook-ingress/scripts/register-github-app.sh <GITHUB_ORG> \
  --app-id <APP_ID> --pem-path /path/to/key.pem --client-secret <SECRET>
```

### Already have an App you must reuse?

Enterprises frequently cannot let a tool create org-owned Apps. That case is fully
documented — permissions, event subscriptions, callback URLs, visibility, and what breaks
when each one is wrong:

→ **[`docs/bring-your-own-github-app.md`](bring-your-own-github-app.md)**

### Then install it on your repos

App settings → *Install App* → your org → **Only select repositories** → pick every repo
agents should work on.

> **An installed-but-uncovered repo is the #1 "agents don't trigger" cause.** A mention in
> a repo the installation does not cover never reaches ADP at all — there is no delivery,
> no log line, and nothing in Agent Activity. Check coverage first, always.

Login-specific configuration (allowlist modes `org` / `explicit` / `open`, callback URLs,
troubleshooting) lives in [`docs/admin/github-sign-in.md`](admin/github-sign-in.md).

<!-- TODO(#4016, #4017): when the onboarding verification card (#4016) and GitHub App
     config-drift re-validation (#4017) ship, document the post-install verification UI
     here — replacing the manual probes in Step 7 with the in-product check. Do not
     document that UI before it merges. -->

### Verify it — do not trust the success message

```bash
./platform/scripts/verify-github-wiring.sh --installation-id <id>
```

Find the installation ID at <https://github.com/settings/installations>, or in the URL
after completing the install. Steps 1–4 are read-only and always run; add `--repo
<owner/name> --issue <n>` to include a real end-to-end dispatch round-trip.

**Run this even though the UI said "success."** It exists because a real PoV deployment
passed the *old* Phase 9 gate with dispatch 100% broken: a per-tenant secret was missing,
so worker pods spawned, crash-looped during bootstrap, and never replied — which looked
like a pass to a check that only asserted "a pod spawned." Every check in this script is
one that failure would have tripped.

Today, GitHub setup is **fail-soft**: register / wire / install can each report success
while login, webhooks, or routing are actually broken. Two issues are open to fix that —
an end-to-end onboarding verification card
([#4016](https://github.com/aws-e/adp/issues/4016), *coming*) and continuous re-validation
of App settings against GitHub, including the callback URL
([#4017](https://github.com/aws-e/adp/issues/4017), *coming*). Until they land, **verify
manually** with the probes in [Step 7](#step-7--troubleshooting-the-things-that-actually-break)
rather than trusting a success message.

---

## Step 4 — Your first agent run

### Write the issue properly — this is the actual skill

An agent's entire world is the issue body. It has no hallway context, no tribal knowledge,
and no way to ask a clarifying question mid-run. A thin issue does not produce a thin
result; it produces a *confidently wrong* result, because the agent will invent the design
you failed to specify.

This repo mandates a five-section issue format (plain-terms opening, Description, Impact
analysis, Design, Deployment, Validation) for exactly that reason. The convention and the
rationale — including real incidents caused by each missing section — are in
[`CLAUDE.md`](../CLAUDE.md) under *Issue-authoring convention*.

The short version of why each section exists:

| Missing section | What the agent does instead |
|---|---|
| Design | Invents a design, often colliding with existing code |
| Deployment | Assumes the wrong CI workflow fires; nothing actually deploys |
| Validation | Declares success on a broken feature |
| Impact analysis | Breaks a surface it did not know existed |

### Trigger the agent with a mention

Comment the persona's **mention string** on the issue:

```
@agent-developer please implement this issue.
```

> ### Use the mention. Not a label.
>
> The `@agent-<persona>` **mention** is the trigger path for human operators, and it is
> the one you want for open-ended agent work.
>
> **Do not add `agent-developer`-style labels to issues to summon an agent.** Labels are a
> *different* dispatch system — in fact two of them, whose label names conflict (bare
> `developer` for webhook ingress vs. `agent-`-prefixed `agent-developer` for ARC/GitHub
> Actions). Reaching for a label when you meant a mention causes duplicate, mis-routed, or
> stuck runs. A real incident: labels applied at issue-creation time implemented *every
> wave of an EPIC at once*, bypassing the intended sequencing.
>
> Labels have a legitimate use — see [Step 8](#arc-pipelines-vs-webhook-agents) — but it is
> not "trigger an agent to go do some work." (The one label-based *entry* point is
> `aidlc-intent` on a newly filed issue, which starts the AIDLC workflow specifically.)

Two mention behaviors worth internalizing now:

- **There is no fan-out.** Mentioning two agents in one comment dispatches exactly **one**
  — the first match. To summon two agents, post two comments.
- **Mentions match as substrings, anywhere.** A mention inside a code block or a quoted
  line still triggers. Do not paste trigger strings into an issue body you do not want to
  dispatch.

### Watch it run

**Dashboard → Agent Activity** (`/activity`; per-run detail at `/runs`).

Expect, in a covered repo: an "Agent started" comment within ~1 minute, an implementation
plan shortly after, and a PR within roughly 5 minutes for a small issue. The agent works
on a branch named `agent/issue-<N>`.

That branch name is a contract, not a style choice: the reviewer-trigger workflow only
fires on head refs matching `agent/issue-*`. A differently-named branch opens a PR that
silently never gets reviewed.

### Nothing happened?

Go to [Step 7](#step-7--troubleshooting-the-things-that-actually-break) — Agent Activity
now tells you *why*, which it did not used to.

---

## Step 5 — Know your agents

→ **[`docs/agent-catalogue.md`](agent-catalogue.md)** is the authoritative list of every
persona ADP ships, with the exact mention string for each.

Treat it as exhaustive in both directions: **if a persona is not in that table it does not
exist, and if a trigger string is not in that table it dispatches nothing.** The catalogue
is kept in parity with the routing code
(`modules/agent-factory/webhook-ingress/lambda/common/personas.py`) by a CI test that fails
when they drift, so it is trustworthy in a way that hand-maintained docs are not.

The four you will use in week 1:

| Mention | Use it for |
|---|---|
| `@agent-developer` | "Implement this issue." The default for any code change. |
| `@agent-reviewer` | PR review. Auto-triggered on `agent/issue-*` branches. |
| `@agent-architect` | Design review of an issue *before* implementation. |
| `@agent-operations` | Deploys, Terraform applies, infra debugging. |

Read the catalogue's **Per-persona constraints** section before relying on any other
persona — some have real caveats (one is currently known-broken, one is mention-only, one
is human-gated by design).

---

## Step 6 — Governance

### Roles

Four roles, defined in `modules/gateway/src/admin/config.py`:

| Role | Scope |
|---|---|
| `platform_admin` | Full platform access (unscoped) |
| `org_admin` | Organization-scoped |
| `dept_admin` | Department-scoped |
| `member` | Least-privilege default — no admin authority |

Two properties worth knowing before you hand out roles:

- **Assignment has a ceiling.** A caller may only assign roles at or below their own
  privilege rank — an `org_admin` cannot mint a `platform_admin`.
- **Platform authority does not come from a tenant membership.** A membership row is
  scoped to one tenant by construction, so a `platform_admin` string in a membership row
  resolves to org-level authority only. Unscoped platform admin comes from the token's
  admin claim alone. This is a deliberate anti-escalation boundary; do not try to grant
  platform admin by editing a membership.

### Access requests

New users hitting the dashboard land on a pending-approval page rather than getting in.
That is correct behavior, not a bug.

Approve them at **Dashboard → `/admin/access-requests`**. Backing API:
`GET /admin/access-requests`, then `POST /admin/access-requests/{request_id}/approve` or
`/deny`. Approval is what atomically creates the org / tenant / dept / team / user rows —
it is the same code path `bootstrap-admin.sh` reused to seed you in
[Step 2](#step-2--become-the-first-admin).

Who may log in *at all* is a separate, earlier gate: the GitHub sign-in allowlist (`org` /
`explicit` / `open` modes) in [`docs/admin/github-sign-in.md`](admin/github-sign-in.md).
`org` mode is the right default for a team.

> **⚠️ Allowlist mode `open` has a known outage mode.** An environment left on
> `ALLOWLIST_MODE=open` without `ALLOW_OPEN_SIGNUP=true` fails **every** GitHub sign-in
> — including yours — while `/api/health` stays green and pods stay Running, so it does
> not look like an auth problem. Recovery and the durable fix (move to `mode=org`):
> [`docs/runbooks/github-auth-allowlist-remediation.md`](runbooks/github-auth-allowlist-remediation.md).

### Budgets and cost visibility

→ **[`modules/gateway/docs/budget-ratelimit.md`](../modules/gateway/docs/budget-ratelimit.md)**
is the primary reference: the cascading Org → Department → Team → User model, budget
periods, and the two enforcement modes — **SOFT** (warn via an `X-Budget-Warning` response
header) vs **HARD** (reject with HTTP 429). Pick the mode deliberately; SOFT budgets are
observability, not a spend cap.

Set them at **Dashboard → `/budgets`** (rate limits at `/ratelimits`). Budgets attach per
entity, so you can cap a team without capping the platform, and limits are independent
while tracking aggregates upward.

Backing API: `GET|POST /admin/organizations/{org_id}/budgets`,
`GET|PUT /admin/organizations/{org_id}/budget/{entity_type}/{entity_id}`, and spend over
time via `GET /admin/organizations/{org_id}/usage/timeseries`. Reading budgets and updating
them are distinct permissions (`budget:read` / `budget:update`), so cost *visibility* can
be granted without granting the ability to raise a cap.

**Set a budget before you hand agent access to a team, not after.** Agent runs invoke
frontier models in a loop; an unbounded persona pointed at a large repo is the expensive
failure mode here.

---

## Step 7 — Troubleshooting: the things that actually break

### "My agent didn't fire"

**Check Agent Activity first — it now tells you the reason.** A delivery that produced no
agent run gets a terminal status of `no_op` (nothing asked for work), `blocked` (a guard
stopped the spawn), or `skipped` (a redelivery was deduplicated), *and* a specific reason
rendered in plain language. Before [#4020](https://github.com/aws-e/adp/issues/4020) this
was a bare "✗ No-op" badge and the answer only existed in Lambda logs; you no longer need
to go spelunking.

The reasons you are most likely to see, and what to do:

| Reason shown | What happened | Fix |
|---|---|---|
| No agent was mentioned | The comment had no `@agent-<persona>` | Check the exact mention string in the [catalogue](agent-catalogue.md) |
| The label applied is not mapped to any persona | You labelled instead of mentioning, with an unmapped label | Mention instead ([Step 4](#step-4--your-first-agent-run)) |
| The GitHub App installation could not be identified | No credentials for that repo | Repo not covered by the installation — [Step 3](#step-3--connect-github) |
| This PR is not on an agent branch | Head ref is not `agent/issue-*` | Rename the branch to the required form |
| The agent mentioned itself / was re-triggered by its own activity | Loop guard | Working as designed |
| Two agents were triggering each other | Cross-persona loop guard | Working as designed |
| Chain depth limit | Agent-triggers-agent chain hit its cap | Working as designed |
| A merged PR already exists for this work | Duplicate delivery | Working as designed |

Enum source of truth:
[`lambda/common/skip_reasons.py`](../modules/agent-factory/webhook-ingress/lambda/common/skip_reasons.py);
the prose mapping is
[`frontend/src/utils/skipReason.ts`](../modules/gateway/frontend/src/utils/skipReason.ts).

**If Agent Activity shows nothing at all**, the webhook never arrived — this is a delivery
problem, not an agent problem. Check, in order:

1. **GitHub → App settings → Advanced → Recent Deliveries.** No delivery at all ⇒ the repo
   is not covered by the installation, or the webhook URL is wrong. `401`/`403 invalid
   signature` ⇒ webhook-secret mismatch.
2. **The ingress is live** — an *unsigned* POST returning **401 is the correct, healthy
   answer** (it proves API Gateway → Lambda works and HMAC verification is on). The exact
   probe is in the quickstart's webhook-ingress section.
3. **The agent-worker image exists.** Terraform never validates it, so a missing
   `adp-agent-runtime` image surfaces only as `ImagePullBackOff` on your first real agent
   run: `kubectl get scaledjobs -n adp-agents` (namespace is `adp-agents`, despite one
   README saying otherwise).

### "The agent started, then went silent"

Two account-level blockers cause a run to reach `Session initialized` and then hang
forever with no error, because the first Bedrock call never completes:

- **Bedrock model access not enabled** for the model the worker invokes. Fix with
  `platform/scripts/enable-bedrock-models.sh` (CLI — the old "console-only" note is
  outdated). Takes ~2 min to propagate.
- **An `execute-api` VPC interface endpoint hijacking DNS**, which turns every API Gateway
  call inside the VPC into a blanket `403`. Fixed in current IaC; accounts deployed earlier
  need the endpoint removed.

A third, subtler variant: the wrong inference-profile prefix returns HTTP 200 with an empty
event stream, so the SDK waits forever with no error at all. Diagnose with a direct
`bedrock-runtime invoke-model` call — not `list-inference-profiles`, which lies about
usability.

All three, with exact diagnosis commands, are in the quickstart's webhook-ingress section.

### "Login is broken"

- **Error on GitHub's own authorize page, never reaching ADP** ⇒ callback URL mismatch.
  Fix it on GitHub; there will be no ADP logs to read, because the request never arrived.
- **Bounced back to the ADP login page with `?error=`** ⇒ a *different* failure: the
  callback did reach the broker, but state verification, the token exchange, or the org
  allowlist rejected it.
- **GitHub 404 on the authorize page** ⇒ the App is private and you are not a member of
  its owner org.
- **Every sign-in fails while health checks stay green** ⇒ the allowlist misconfiguration
  in [Step 6](#step-6--governance).

Full matrix: [`docs/admin/github-sign-in.md`](admin/github-sign-in.md) §Troubleshooting,
and the what-breaks-when-something's-missing table in
[`bring-your-own-github-app.md`](bring-your-own-github-app.md) §6.

Nothing here re-validates itself yet — App settings can drift out from under you after
registration ([#4017](https://github.com/aws-e/adp/issues/4017), *coming*).

### "The frontend is a blank page"

Built with the wrong API prefix. It must be `/api`, **not** `/api/gateway` — the wrong
prefix makes every SPA call hit the S3 HTML fallback, which returns **HTTP 200** with HTML
and crashes the dashboard.

This is the reason for a rule worth adopting permanently: **when probing an ADP API, assert
the JSON body, never the status code alone.** Status-code-only checks have masked real
deploy incidents, because the SPA fallback happily returns 200 for everything.

Deploy-time failure modes (CrashLoopBackOff, CloudFront 502, EKS nodes not appearing,
CodeBuild) are catalogued in [`CLAUDE.md`](../CLAUDE.md) §Troubleshooting Reference and in
the quickstart's per-phase gotchas.

---

## Step 8 — Best practices

### Size the work correctly

The most common self-inflicted failure is **an issue body that is too big.** The worker
assembles its prompt from the issue body plus persona plus rules; a ~14.5KB body produced a
~45KB prompt, and the agent came online, ran a few turns, and the process died ~58s in with
no error — repeatably. A 1KB issue doing the same *kind* of work ran fine.

Recognize the symptom: the agent posts "Agent running" once, then goes silent and never
posts a plan. If the issue is large, suspect size before anything else.

So: **detail lives in child issues; orchestrators only sequence and link.** For
multi-issue builds, use a lean index issue plus small per-wave mini-orchestrators (~1–3KB
each), and never let an orchestrator issue get its own `agent/issue-<N>` work branch.

→ **[`docs/orchestration-issue-guide.md`](orchestration-issue-guide.md)** — written from
failures that each actually happened. Read it before your first multi-issue build, not
after.

For genuinely large programs: [`docs/developing-at-epic-scale.md`](developing-at-epic-scale.md).

### Review flow

- Agents work on `agent/issue-<N>` and open a PR; the reviewer agent auto-triggers on that
  branch pattern.
- `@agent-reviewer` blocks on real issues and suggests on style. **Human merge remains the
  gate** — review agent output as you would a new contributor's: the code compiles and
  passes tests, but the *design intent* is yours to verify.
- Agents run pre-submit lint and tests for the modules they touch (per-module commands are
  in [`CLAUDE.md`](../CLAUDE.md) §Pre-submit checks). A red CI on an agent PR is a real
  signal, not noise.
- Want a design pass before implementation? Send the issue to `@agent-architect` first,
  then `@agent-developer`. Cheaper than reviewing a wrong implementation.

Coding standards agents are held to:
[`docs/agent-coding-guidelines.md`](agent-coding-guidelines.md) — think before coding,
simplicity first, surgical changes, goal-driven execution. The hard rule: every changed
line must trace to an acceptance criterion in the issue.

### ARC pipelines vs webhook agents

Two execution models, and picking the wrong one wastes real time:

| | **Webhook agents** (mention) | **ARC pipelines** (label) |
|---|---|---|
| Path | GitHub webhook → Lambda → SQS → KEDA → agent-worker pod | `issues.labeled` → GitHub Actions on self-hosted EKS runners |
| Good for | Open-ended reasoning: "implement this", "review this", "debug this" | Deterministic, auditable, repeatable step sequences |
| Trigger | `@agent-<persona>` mention | `agent-`-prefixed label |
| Use when | You want an agent to figure it out | You want the *same steps* every time (e.g. multi-stage deploys) |

**Default to webhook agents.** ARC is not deprecated and is not a legacy path — it is the
right tool when you need a known, auditable sequence rather than agent judgment, and an
agent can trigger and monitor an ARC pipeline itself. Setup and usage:
[`modules/agent-factory/SETUP-GUIDE.md`](../modules/agent-factory/SETUP-GUIDE.md).

### Operating habits that pay off

- **Assert response bodies, not status codes** (see the blank-page section above).
- **Every deploy step is idempotent — re-run it.** Scripts here are written to be
  re-runnable; re-running is usually safer than hand-patching.
- **Set budgets before granting agent access**, not after the bill arrives.
- **File follow-ups instead of scope-creeping a PR.** Agents are instructed to do this;
  hold humans to it too.

---

## Where to go next

| I want to… | Read |
|---|---|
| Deploy (self-managed) | [`deploy-quickstart.md`](adp-platform-deployment/deploy-quickstart.md) |
| Check ADP-managed cross-account status | [`adp-managed-deploy.md`](adp-platform-deployment/adp-managed-deploy.md) |
| Know every persona | [`agent-catalogue.md`](agent-catalogue.md) |
| Run a multi-issue build | [`orchestration-issue-guide.md`](orchestration-issue-guide.md) |
| Understand the architecture | [`ARCHITECTURE.md`](../ARCHITECTURE.md) |
| Use my own GitHub App | [`bring-your-own-github-app.md`](bring-your-own-github-app.md) |
| Configure GitHub sign-in | [`docs/admin/github-sign-in.md`](admin/github-sign-in.md) |
| Validate what I deployed | [`deployment-manifest.md`](adp-platform-deployment/deployment-manifest.md) |
| Tear it all down | [`CLAUDE.md`](../CLAUDE.md) §Destroy / Teardown (`undeploy.sh`) |
