# #4559 — How CI dispatches ADP work: EventBridge origin, service identity, tenant mapping

**Status:** ruled (spike; produces this note + a wiring checklist for U11)
**Issue:** [#4559](https://github.com/aws-e/adp/issues/4559)
**Blocks:** U11 [#4450](https://github.com/aws-e/adp/issues/4450) (autonomous ops handoff / drive-to-merge)
**EPIC:** intent #4290 · build plan #4438 · units U9 #4448, U10 #4449
**Builds on:** #2154 (EventBridge machine/root transport), #2152 + #4128 (`/agent/trigger` semantics), #4233 (org gate, from #4071), #4129 (server-side provenance), #4344 (service-principal id namespace)

---

## RULING (summary)

| # | Question | Ruling |
|---|---|---|
| 1 | Transport | **Confirmed.** `aws events put-events` → default-bus rule + InputTransformer → webhook Lambda. Direct-SQS and `/agent/trigger` stay closed. |
| 2 | Rule / persona | **One rule**, `adp-<env>-security-agent-dispatch`, dispatching **`operations` only** (one root per night). Architect grouping and per-item delivery are **in-run `adp-trigger` hops, not EventBridge rules.** |
| 3 | Service identity | Key `eventbridge:adp-<env>-security-agent-dispatch`. **Seeded as a Terraform `aws_dynamodb_table_item`**, not a script. `allowed_personas = ["operations"]`. |
| 4 | Tenant attribution | **Tenant is a property of the registration row, never of the event.** Set `org_id = tenant_id = ` the GitHub org that owns the pipeline repo (`aws-e` in dev). This is not a new tenant kind — it reuses the org anchor from #2951. |
| 5 | Runner IAM | New `events:PutEvents` statement in `runner_base`, `Resource = arn:aws:events:us-east-1:*:event-bus/default`. **Not least-privilege at the source level — AWS cannot express that.** See §5 for the compensating control, which is the load-bearing part. |
| 6 | Cost owner | `root_human_id` = the service key, `is_human_rooted=false`. The gateway already namespace-qualifies this to `service:<key>` for the `ROOT_USER` budget entity (#4344, shipped). **No new work; one config decision: the nightly's budget ceiling.** |

**Three findings change U11's scope** and are the real output of this spike (§7):

- 🔴 **A machine-rooted run gets `authorized_user_id=""` — no vault credentials, structurally.** U11's delivery agents cannot use any tenant credential. This is a deliberate #3174 policy, not a bug, and it is not mentioned in #4450.
- 🔴 **`target.create_issue: true` has no consumer on this path.** The EventBridge → worker chain requires a real `source_ref.issue`; the worker does `gh issue view $ISSUE_NUMBER` and dies without it. The CI job must create the issue itself and pass `issue_number`.
- 🟠 **The `dedup_key` field does not deduplicate anything.** It only names the channel key. SQS dedup is keyed on `arrived_at`, which differs per call.

---

## 1. Transport — confirmed, with the premise corrected

The issue asks to "confirm the transport." Confirmed. But one framing in the issue body needs correcting, because it changes the size of U11.

The issue describes the pipeline emitting "**one** `aws events put-events` call" and U11 (#4450) describes "exactly one dispatch per night… **and** per-item dispatches bounded by the number of items." Those are consistent only if the per-item dispatches are *not* EventBridge calls. They are not, and they must not be:

**EventBridge is the transport for the *root* only.** Every subsequent hop — architect grouping (U9), operations → developer per item (U11) — is an **in-run `adp-trigger` dispatch**, which is the committed mechanism in intent #4290 ("agent → agent dispatch is `adp-trigger --persona <p> --issue <N>`, NEVER an `@agent-<persona>` comment and NEVER a label").

This matters because the two paths have opposite lineage semantics, and picking the wrong one silently destroys the chain:

| Property | EventBridge (`put-events`) | `adp-trigger` (`/agent/trigger`) |
|---|---|---|
| Mints a new root | **Yes** — `is_new_chain=True`, `chain_depth=0` | **Never** — `422 unknown_chain` (`agent_trigger.py:139`) |
| Lineage source | Service identity row | Server-resolved from `correlation-index` GSI (#4129) |
| `chain_depth` behaviour | Forced to 0 (`spawn_persona.py:297-300`) | Inherited + 1 (`:304-321`) |
| Correct use | Nightly origin, once | Every hop inside the night |

If U11 used `put-events` per item, **every item would be its own root at `chain_depth=0`**. The chain-depth cap (`MAX_CHAIN_DEPTH=8`) and the cross-persona loop guard would never engage, and the night's runs would not be linked in lineage — defeating #4450's own acceptance criterion that "run lineage for the night shows exactly one delivery-role dispatch."

### The two rejected routes, re-verified

Both are genuinely closed. The issue's reasoning is right.

| Route | Status | Verified against |
|---|---|---|
| CI posts to SQS directly | **Closed.** The queue has no guard layer; `publish_envelope` takes a plain dict and calls `send_message` (`sqs_publisher.py:78`). Every control — org gate, `allowed_personas`, rate limit, depth cap, provenance row — lives *above* it in `spawn_persona()`. Any SQS producer forges identity by construction. | `lambda/common/sqs_publisher.py:34-78`, `lambda/common/spawn_persona.py:120-190` |
| CI calls `/agent/trigger` | **Closed, by design and by explicit rule.** A fresh call has no `correlation_id` → `400 missing_lineage`; a fabricated one → `422 unknown_chain`. The docstring states the rule: "NEVER mint a new root from agent source." | `lambda/github/agent_trigger.py:19-20, 116, 139` |

**Do not "fix" `/agent/trigger` to accept a root.** That was the deliberate outcome of #4128's fail-closed work; the 422 is the control.

---

## 2. The rule, the target, and which personas

### 2.1 One rule, one persona

Add **one** rule dispatching **`operations`** only.

The tempting alternative — one rule per persona, or a persona passed through from the event — is wrong for a specific reason: **`persona` in the InputTransformer is a Terraform-authored literal, but anything sourced from `$.detail` is caller-controlled.** The handler validates `persona` against `VALID_PERSONAS` and `allowed_personas` (`handler.py:110, 131`), so a caller cannot spawn an *unregistered* persona — but with `allowed_personas` listing several, any principal holding `events:PutEvents` picks freely among them. Pinning the persona as a literal in the rule and to a single entry in `allowed_personas` makes the choice a Terraform-reviewed decision instead of a runtime input.

U9 (#4448) needs architect runs, so why not a second rule? Because **U9's architect runs are dispatched from inside the night's chain**, not from CI — and #4448 is explicit that "nothing authored here dispatches anything," with a gate asserting the persona doc retains its dispatch prohibition. The CI job's only job is to start `operations`; `operations` sequences everything else via `adp-trigger`.

> **If a later unit genuinely needs a second machine-originated persona**, add a *second rule* with its own service identity and its own single-entry `allowed_personas` — do not widen this one. One rule : one persona : one identity keeps the blast radius of a leaked `PutEvents` grant to one persona.

### 2.2 The block to add

`modules/agent-factory/webhook-ingress/infra/eventbridge.tf`, modeled on `alarm_state_change` (`:36-96`). Gated on a new `var.enable_eventbridge_security_agent_rule` (default `false`), matching the existing style at `variables.tf:317-333`.

```hcl
resource "aws_cloudwatch_event_rule" "security_agent_dispatch" {
  count = var.enable_eventbridge_security_agent_rule ? 1 : 0

  name        = "adp-${var.environment}-security-agent-dispatch"
  description = "Nightly security pipeline -> webhook ingress Lambda (root dispatch, #4559)"

  # source is the ONLY match condition the emitter controls; detail-type pins
  # the shape the transformer below assumes.
  event_pattern = jsonencode({
    source      = ["adp.security-agent"]
    detail-type = ["ADP Agent Dispatch"]
  })

  tags = { Purpose = "agent-dispatch", Issue = "4559" }
}

resource "aws_cloudwatch_event_target" "security_agent_to_lambda" {
  count = var.enable_eventbridge_security_agent_rule ? 1 : 0

  rule      = aws_cloudwatch_event_rule.security_agent_dispatch[0].name
  target_id = "webhook-ingress-lambda"
  arn       = aws_lambda_function.github_webhook.arn

  input_transformer {
    input_paths = {
      source       = "$.source"
      detail_type  = "$.detail-type"
      reason       = "$.detail.reason"
      run_date     = "$.detail.run_date"
      issue_number = "$.detail.issue_number"
    }

    # persona and service_identity are TERRAFORM LITERALS, never sourced from
    # $.detail — see 2.1. repo is a literal for the same reason: it is the
    # #4233 org-gate anchor and must not be caller-supplied.
    input_template = <<-EOF
      {
        "source": <source>,
        "detail-type": <detail_type>,
        "detail": {
          "adp_trigger": {
            "persona": "operations",
            "service_identity": "eventbridge:adp-${var.environment}-security-agent-dispatch",
            "reason": <reason>,
            "dedup_key": <run_date>,
            "target": {
              "repo": "${var.eventbridge_security_agent_repo}",
              "issue_number": <issue_number>
            }
          }
        }
      }
    EOF
  }
}
```

**No new `aws_lambda_permission` is needed.** `eventbridge_invoke` (`eventbridge.tf:22-28`) is already bus-wide for `rule/adp-${var.environment}-*`, and this rule matches that prefix.

**`issue_number` is required, not optional** — see §7.2. `<issue_number>` interpolates unquoted, so it arrives as a JSON number, which is what `payload["issue"]["number"]` needs.

---

## 3. Service identity — seed it in Terraform

**Key:** `eventbridge:adp-<env>-security-agent-dispatch` — i.e. `eventbridge:<rule-name>`, the convention the alarm example already uses (`eventbridge.tf:80`) and which `service_identity.py:9` documents. No new convention.

**Seed it as Terraform, not a script.** Today **zero `service_account` rows exist in any `.tf`**. The only seeding in the repo is `scripts/smoke-eventbridge.sh:43-53`, which `put-item`s a row after **scanning for any existing installation row to borrow `tenant_id`/`org_id` from** (`:35-40`). That is a smoke-test hack — its own note says "safe to delete" — and it must not become the production path. Borrowing an arbitrary scanned org is precisely the cross-tenant misattribution the issue's impact table warns about.

```hcl
resource "aws_dynamodb_table_item" "security_agent_service_identity" {
  count = var.enable_eventbridge_security_agent_rule ? 1 : 0

  table_name = var.identity_index_table_name
  hash_key   = "identity_type"
  range_key  = "identity_value"

  item = jsonencode({
    identity_type    = { S = "service_account" }
    identity_value   = { S = "eventbridge:adp-${var.environment}-security-agent-dispatch" }
    tenant_id        = { S = var.eventbridge_security_agent_org }
    org_id           = { S = var.eventbridge_security_agent_org }
    allowed_personas = { L = [{ S = "operations" }] }
  })
}
```

Why Terraform:

1. **The rule and its identity must land together.** A rule without its row fails closed at `403 unknown_service_identity` (`handler.py:128`) — the failure is safe but silent, visible only in Lambda logs. Nothing retries, and the night reports nothing.
2. **`allowed_personas` is a security control**, so it belongs in reviewed, drift-detected config.
3. It makes the `tenant_id` decision (§4) an explicit reviewed value rather than whatever a scan returned.

**Two caveats for the implementer:**

- `aws_dynamodb_table_item` manages *only the attributes it declares* and will fight anything else that writes the same key. That is acceptable here (nothing else writes `service_account` rows) but must be stated in the PR.
- The table is **owned by a different Terraform state**: `aws_dynamodb_table.identity_index` is in `modules/gateway/infra/main.tf:1297`, while this item would be created from `webhook-ingress/infra` via the existing `var.identity_index_table_name`. Writing an item into a table owned by another state is fine (no resource dependency), but note the ordering: gateway-infra must have applied first. Also see **#4042** (open — dropping the dev-default for `identity_index_table_name`) and **#3331** (open — identity-index generalization); this row must not contradict either.

---

## 4. Tenant attribution — the open question

**Ruling: tenant is a property of the registered service identity, never of the event. Set `org_id = tenant_id = ` the GitHub org that owns the pipeline repo — `aws-e` in dev.**

### Why the question is narrower than it looks

The issue frames this as open ("what tenant a machine-started job belongs to"). The mechanism has already constrained the answer to one shape — the *value* is what's undecided:

1. `resolve_service_identity()` reads `tenant_id` and `org_id` **off the row** (`service_identity.py:93-97`). The event body cannot influence them. There is no code path by which a caller supplies a tenant.
2. The handler then calls `resolve_installation_for_tenant(identity_result.org_id)` and returns **`422 no_installation_for_tenant`** if there is no reverse row (`handler.py:202-208`). So **`org_id` must be a real GitHub org with the App installed.** This eliminates every synthetic-tenant option — `"system"`, `"platform"`, a null tenant, or a `service-*` namespace would all 422 immediately.

So the only real choice is *which real org*, and for a pipeline whose output is issues and PRs in `aws-e/adp`, that is the org owning that repo.

### Why this is not a new tenancy concept

Per #2951 (`docs/design-notes/2951-github-org-to-adp-tenant.md`) the org login *is* the tenant anchor for org-owned tenants. The service identity is a **machine principal inside an existing tenant**, not a tenant of its own. This keeps three committed controls intact:

| Control | Why it holds |
|---|---|
| Org gate (#4233) | `org_id` is a real org login, never absent and never the placeholder `"default"` — the two fail-closed conditions. |
| Cross-tenant checks | `target.repo` is a Terraform literal in the same org as `org_id`; nothing caller-supplied crosses a tenant boundary. |
| Installation ownership | The `org_installation` reverse row already exists for `aws-e` (written by `_auto_register_installation`), so dispatch resolves. |

**Set `tenant_id` and `org_id` to the same value.** Elsewhere in the codebase `tenant_id` and `org_id` are synonyms for org-owned tenants (the smoke script does this too, `:47-48`). Do not invent a divergence here — `spawn_persona` receives both (`handler.py:230, 232`) and a mismatch would produce rows whose tenant and org disagree, breaking the tenant-index GSI queries the Activity UI runs.

### What is deliberately *not* claimed

`is_human_rooted=false` and `root_human_id=<service key>` remain, per #2154. Per #4129, the authoritative provenance record is the server-written `webhook-events` row read via `correlation-index` — **not** the pod-writable `correlation-pointers` row. This note does not weaken that: the CI job supplies no provenance fields at all, so there is nothing to forge. This is a *stronger* position than the #4303 ruling had to defend, since there the tick transported a resolved identity; here identity is resolved server-side from a Terraform-seeded row.

---

## 5. Runner IAM — and the honest limit of "least privilege"

The issue asks to "confirm least-privilege (single source/bus)." **Half of that is not achievable, and pretending otherwise would be the bug.**

### What to add

`modules/agent-factory/infra/modules/runner-iam/main.tf`, a new statement in `runner_base` after `EventBridgeRules` (`:443-458`) — that policy is the declared home for EventBridge (`:265`):

```hcl
{
  Sid      = "EventBridgePutEvents"
  Effect   = "Allow"
  Action   = ["events:PutEvents"]
  Resource = "arn:aws:events:us-east-1:*:event-bus/default"
},
```

It cannot join `EventBridgeRules`: that statement's resource is a *rule* ARN, while `PutEvents` authorizes against the **event-bus** ARN. Notes:

- **No permissions-boundary change.** The boundary already carries `events:*` on `Resource="*"` (`main.tf:102`), so it does not deny this. (That wildcard is pre-existing breadth on this role, tracked by #1154 / #4116 — do not widen it further, and do not rely on it: the boundary caps, it does not grant.)
- **Watch the 10,240-byte managed-policy limit.** The base/services split exists for exactly this (`:6-14`, #1204). If `runner_base` is tight, `runner_services` is the fallback.
- Edit `infra/modules/runner-iam/`, **not** `runner-infra/infrastructure/iam.tf` — the latter is a stale near-duplicate referenced only from docs.
- The nightly workflow runs on `arc-runner-org` with **ambient IRSA** and no `configure-aws-credentials`, so this grant is what the job actually gets. The separate `adp-<env>-securityagent-nightly` service role (`platform/infra/securityagent-nightly-iam.tf`) trusts only `securityagent.amazonaws.com` and **cannot be assumed by the runner** — do not put `PutEvents` there.

### The limit, stated plainly

**`events:PutEvents` cannot be scoped to an event `source`.** The IAM resource for `PutEvents` is the bus; `source` is a field in the request body, and there is no IAM condition key for it. So this grant lets the runner emit **any event, with any `source`, onto the default bus** — including a `source` that matches *another* `adp-<env>-*` rule.

That is a real widening and the issue's own impact table names it ("Runner IRSA over-granted → CI role can emit arbitrary `adp_trigger` events for any persona/repo"). It is not fully closable with IAM. What actually contains it:

1. **The transformer is the choke point.** `persona`, `service_identity`, and `repo` are Terraform literals, so an attacker controlling the event body still cannot choose the persona, the identity, or the target repo — only `reason`, `run_date`, and `issue_number`. **This is the primary control, and it is why §2.2 insists those three stay literals.** An implementer who "simplifies" by sourcing `persona` from `$.detail` deletes it.
2. **`allowed_personas` is a second, server-side ceiling** on the identity row.
3. **The blast radius equals the set of enabled `adp-<env>-*` rules.** Today that is one (the alarm rule has never been enabled in any environment — `enable_eventbridge_alarm_rule` defaults `false` at `variables.tf:317-321` and is set in no tfvars). Each future rule widens what a compromised runner can trigger — worth stating in the U11 PR as a standing consequence.

State it as "scoped to the bus, with source-level selection constrained by the transformer," not as "least privilege."

---

## 6. Cost owner

**Ruling: no new work. The mechanism shipped in #4344.**

The chain: `root_human_id` = the service key with `is_human_rooted=false` (`eventbridge/handler.py:159-163`) → the gateway qualifies it to `service:<key>` when writing `attributed_user_id` and keying the `ROOT_USER` budget entity (`enforcement_service.py:798-802`, `_qualify_root_principal_id`, `_SERVICE_PRINCIPAL_PREFIX = "service:"` at `:90`).

Two things follow that U11 should know:

- **The qualification is gateway-side at consumption, not in the handler.** The webhook Lambda writes the bare key. Do not "fix" the handler to write `service:<key>` — the gateway would double-prefix it, and `_unqualify_root_principal_id` (`:438`) assumes the raw form.
- **The one open decision is the ceiling**, not the owner: the nightly gets its own `ROOT_USER` budget envelope keyed `service:eventbridge:adp-<env>-security-agent-dispatch`. #4344 is explicit that "unattended CI is exactly what needs a ceiling." Setting that number needs U7's measured run cost, which does not exist yet — so it is a follow-on, and until it is set the nightly runs under whatever tenant-level default applies. **Flag it; do not let U11 silently assume a cap exists.**

---

## 7. Findings that change U11's scope

These are the substantive output of the spike. None appears in #4450.

### 7.1 🔴 A machine-rooted run has no vault credentials — structurally

`_compute_authorized_user_id()` returns `""` immediately when `is_human_rooted` is false (`spawn_persona.py:616-618`):

```python
is_human_rooted = correlation_ctx.get("is_human_rooted", False)
if not is_human_rooted:
    return ""
```

`""` means no vault access, per the #3174 policy table (`:600-603`). Because every hop inherits `is_human_rooted` from the root, **the entire night's chain — operations and every developer run it dispatches — runs with no tenant credential.**

This is correct policy, not a defect: an unattended machine trigger must not wield a human's credential authority. But U11 must be designed knowing it. GitHub App installation tokens are minted per-run from `installation_id` and are unaffected, so filing issues, pushing branches, and opening PRs all work. What will *not* work is anything reaching for a vault-stored tenant credential (`adp/users/*`, `adp/teams/*`, `adp/orgs/*` — also explicitly Denied to the runner boundary at `runner-iam/main.tf:141-171`).

**Action for U11:** confirm the drive-to-merge loop needs only App-token operations. If any step needs a tenant credential, that is a design change requiring its own issue — not something to paper over by flipping `is_human_rooted`.

### 7.2 🔴 `target.create_issue: true` has no consumer — the CI job must create the issue

The alarm example sets `"create_issue": true` (`eventbridge.tf:85`) and #2154's schema lists it. **Nothing on this path reads it.** The only `create_issue` implementation is in the unrelated chat-ingest Lambda (`agent-factory/gateway/lambdas/ingest/`), which the webhook Lambda never calls.

What actually happens to `target`: only `issue_number` and `repo` are read (`eventbridge/handler.py:187-188`). `issue_number` becomes `payload["issue"]["number"]` (`:196-197`), which is the sole source of `source_ref.issue` (`spawn_persona.py:500-502`). Without it, `source_ref.issue` is `None`.

That is fatal downstream. The agent worker reads `ISSUE_NUMBER` from the env (`agent-worker.ts:104`) and immediately runs `gh issue view ${ISSUE_NUMBER} --json ...` (`:489`). With an empty value the command fails and the run dies before doing anything. The repo already documents this requirement: `environments/dev/modules/gateway.tfvars:114` — "the agent worker **hard-requires** `source_ref.{installation_id, repo, issue}`." The #4303 ruling hit the same constraint from the orchestration side.

**Action for U11:** the CI job creates the night's issue (dated parent / orchestration issue, per U9/U10) **before** `put-events`, and passes its number as `issue_number`. The EventBridge dispatch attaches an agent to an existing issue; it cannot conjure one. Also worth a follow-up: `create_issue` should be dropped from the alarm example and #2154's documented schema, since it reads as supported and is not.

### 7.3 🟠 `dedup_key` does not deduplicate

The name promises idempotency the code does not provide. `dedup_key` is used for exactly one thing — building `channel_key = f"eventbridge:{effective_dedup}"` (`handler.py:174-175`) for correlation-pointer writes. It never reaches SQS. `MessageDeduplicationId` is `f"{arrived_at}_{repo}_{issue}"` (`sqs_publisher.py:73`), and `arrived_at` differs on every call — so **two identical `put-events` calls produce two runs.**

The issue's impact table leans on "dedup" as a control ("bounded by dedup + story count"), and #4438 repeats it. That control does not exist at this layer.

There *is* a partial accidental guard, and its shape is worth knowing because it cuts the other way. Setting `dedup_key = <run_date>` makes the channel key stable per night, so a second dispatch on the same night hits **Guard 3, self-re-trigger** (`spawn_persona.py:349-359`) — same persona as `last_triggered_persona` on that channel → blocked. But that guard only applies to **bot senders** (`:144`), and `_is_bot_sender` tests `type == "Bot"` or a `[bot]`-suffixed login (`:317-324`) while the EventBridge handler sets `type: "Service"` (`:181`). **So `_is_bot_sender` returns False and Guards 2–5 are skipped entirely for this path** — including the depth cap at the root, which is harmless at depth 0, but also including the re-trigger guard that would have absorbed a double dispatch.

**Action for U11:** put idempotency in the CI job — the same place U9 already puts "exactly one dated parent per date, safe on retry." Do not rely on `dedup_key`, and do not describe it as dedup in the issue body. If a server-side guard is wanted later, that is its own issue (candidate: make `_is_bot_sender` treat `Service` as non-human so Guards 2–5 apply, or key SQS dedup off `dedup_key` when present — both are behaviour changes needing their own tests).

### 7.4 🟡 The EventBridge path ignores the configured rate limits

`_get_rate_limiter()` in `eventbridge/handler.py:49-57` constructs `RateLimiter(table_name=..., region=...)` and passes **neither** `limit_per_window` nor `limit_per_hour`, so it silently uses the hardcoded defaults (50/window, 500/hour — `rate_limit.py:35-36`). The GitHub handler does read the env vars (`github/handler.py:1271-1279`), which Terraform injects as `RATE_LIMIT_PER_WINDOW` / `RATE_LIMIT_PER_HOUR` (`lambdas.tf:100-101`) and which `test_rate_limit_env.py` covers.

So an operator lowering the ingress rate limit does **not** lower it for machine triggers. Harmless for one nightly dispatch; wrong as a control surface, and the drift is invisible. Small, self-contained fix — worth its own issue rather than folding into U11.

---

## 8. Wiring checklist for U11

Ordered; each item is independently verifiable.

**Terraform — `webhook-ingress/infra/`**
1. `variables.tf`: add `enable_eventbridge_security_agent_rule` (bool, default `false`), `eventbridge_security_agent_repo` (string), `eventbridge_security_agent_org` (string). Mirror the style at `:313-333`.
2. `eventbridge.tf`: add the rule + target from §2.2. **`persona`, `service_identity`, `repo` stay Terraform literals.** No new `aws_lambda_permission`.
3. `eventbridge.tf` (or `dynamodb.tf`): add the `aws_dynamodb_table_item` from §3.
4. `environments/dev/modules/*.tfvars`: set the three variables for dev. Note the CLAUDE.md tfvars caveat — a `-var-file` entry overrides `TF_VAR_*`, so do not pin values that a workflow injects from SSM.

**Terraform — `agent-factory/infra/modules/runner-iam/`**
5. Add the `EventBridgePutEvents` statement from §5 to `runner_base`. Check the 10,240-byte limit. Beware **#4127** (a pending implicit move currently blocking agent-factory applies) and the stale duplicate in `runner-infra/`.

**CI job — `.github/workflows/security-agent-nightly.yml` + `.github/scripts/`**
6. Create the night's issue first; capture its number. Idempotent per date (§7.3).
7. Emit **one** event: `source=adp.security-agent`, `detail-type=ADP Agent Dispatch`, `detail={reason, run_date, issue_number}`. Nothing security-sensitive in `detail` — it is caller-controlled and lands in a DDB row and CloudWatch logs.
8. **Do not add a `schedule:` key.** `test_securityagent_preflight.py` fails the build on one (`test_workflow_has_no_schedule_key`, `test_schedule_is_not_reachable_through_any_other_trigger`). Arming the cron is an explicit follow-on after wave 4 (#4438).
9. All in-run hops use `adp-trigger --persona <p> --issue <N>`. Never `put-events`, never a mention, never a label (§1, #4450).

**Apply order** (both are `workflow_dispatch`, per CLAUDE.md)
10. `agent-factory-infra-apply.yml` (runner IAM) → `webhook-ingress-deploy.yml` (rule + identity row). The IAM grant must exist before the first emit, or the job fails `AccessDeniedException`. Note `webhook-ingress-deploy.yml` **auto-applies on `infra/**` pushes** — the ordering constraint that shaped #4129's rollout.

**Smoke test** (§ the issue's Validation, refined)
11. From the runner role: `aws events put-events --entries '[{"Source":"adp.security-agent","DetailType":"ADP Agent Dispatch","Detail":"{\"reason\":\"smoke\",\"run_date\":\"2026-09-01\",\"issue_number\":<N>}"}]'`
12. Assert a `webhook-events` row with `is_human_rooted=false`, `tenant_id=<org>`, a fresh `correlation_id`, `chain_depth=0`, and `root_human_id=eventbridge:adp-<env>-security-agent-dispatch`.
13. Assert rejection paths: an unregistered `service_identity` → `403 unknown_service_identity`; a persona outside `allowed_personas` → `403 persona_not_allowed`. Both are only reachable by temporarily editing the transformer or the row — note that, rather than leaving "assert a bad identity is rejected" as an untestable line item.
14. Regression: GitHub-originated and `adp-trigger` dispatch unchanged; no new SQS producer.
15. `scripts/smoke-eventbridge.sh` is the closest existing harness — but it seeds its own throwaway identity by scanning for an org to borrow (§3). Model the *shape* on it; do not reuse its seeding.

---

## 9. Verdict

⚠️ **Ready with caveats** — the transport is confirmed, all six points are ruled, and U11 can build against this note provided three things are accepted:

1. **No vault credentials for the night's runs** (§7.1). If the drive-to-merge loop needs a tenant credential, U11's design changes and needs its own issue.
2. **The CI job creates the issue and passes `issue_number`** (§7.2). `create_issue: true` is inert on this path.
3. **Idempotency lives in the CI job, not in `dedup_key`** (§7.3), and #4450 / #4438 should stop citing platform dedup as the fan-out bound.

Two follow-ups fall out, neither blocking: the rate-limit env drift (§7.4), and removing `create_issue` from the alarm example and #2154's documented schema.
