# #4010 rev 2 — closing an atomicity gap between the k8s and Terraform layers

**Date:** 2026-08-24 · **Issue:** #4010 · **PR:** #4138 · **Commit:** `968056c`
**Context:** rev 1 of the PR was reviewed and found to contain a blocking
rollout-outage bug. This file covers the fix, and is a companion to
`2026-08-24-issue-4010-learnings.md` (the spike findings).

## The class of bug — worth internalizing, it will recur

**A change that spans the k8s layer and the Terraform layer cannot be made
atomic by any single apply.** They have different triggers, different gates, and
no shared transaction. If change A (k8s) is only safe once change B (Terraform)
has landed, then shipping A in a file that CI applies unconditionally guarantees
a window where A is live and B is not — regardless of how carefully B is guarded.

Rev 1 got this wrong in a way that *looked* safe. Its Terraform var defaulted to
empty and fell back to the old ALB, which is genuinely correct in isolation. The
tftest suite validated exactly that and passed 5/5. But the k8s deny changed what
the edge ALB *did* with the traffic the fallback pointed at, and no Terraform-layer
test can model that. **Passing tests on the layer you're thinking about are not
evidence about the layer you aren't.**

The tell: I wrote "empty vars = pre-#4010 behavior, no window where /internal
503s" while the same PR also changed an unconditionally-applied manifest. Any
claim of the form "no window" needs to name *every* layer that ships in the
change, not just the one with the guard.

## Concrete mechanism (for anyone touching this deploy path)

Rev 1's failure chain on an established environment:

1. Merge → `gateway-deploy.yml:310-321` loops `modules/gateway/k8s/*.yaml` and
   applies everything. Edge ALB starts 403-ing `/internal`.
2. `gateway-deploy.yml:344-352` **`exit 0`'d early** when the edge ALB was
   already in the SSM cache → internal-plane discovery never ran.
3. `newly_wired=false` → the `gateway-infra-apply` trigger (gated on
   `newly_wired == 'true'`) was skipped.
4. `internal_plane_alb_dns` stayed empty → Terraform fallback kept
   `/internal/{proxy+}` pointed at the edge ALB → which is now denying it.
5. Result: all SigV4 internal calls 403 indefinitely. No self-healing.

**Second, independent gap** (found by grepping rather than assuming the trigger
was the only problem): `gateway-infra-apply.yml` never passed
`TF_VAR_internal_plane_alb_*` at all. `wire-gateway-alb.sh --no-wait` was already
emitting those `$GITHUB_OUTPUT` values — they were just never consumed. So the
repoint was unreachable from the merge path *entirely*; it only ever happened via
`wire-gateway-alb.sh --apply` or `deploy-all.sh`. **Lesson: when a wiring script
grows new outputs, grep every consumer.** Emitting an output and consuming it are
separate changes and the gap is silent — no error, just a var that stays empty.

```bash
# The grep that found it — do this whenever you add a TF_VAR to a script:
grep -rn "internal_plane" .github/workflows/   # returned nothing
```

## The gating pattern (reusable)

When k8s change A must follow Terraform change B:

1. **Get A out of the unconditional apply path.** Both loops here glob
   `k8s/*.yaml` **non-recursively**, so `k8s/patches/` is invisible to them.
   Verify it, don't assume:
   ```bash
   for f in k8s/*.yaml; do echo "$f"; done | grep -i patch   # must be empty
   ```
   A partial Ingress left in `k8s/` would also be applied blind and
   three-way-merge away the real `spec.rules`.
2. **Gate A on live observable state, not on the state store.** I check the
   actual API-GW integration URI, not the SSM param or Terraform state. SSM says
   "someone intended this"; the live integration says "it landed". Those diverge
   exactly when it matters (async `gh workflow run` still in flight).
3. **Make skipping the safe default** — exit 0 with a notice, so the next deploy
   retries. Never blind-apply when the precondition can't be read.
4. **Ship an assertion for the forbidden state**, not just the happy path. The
   invariant here ("deny live AND integration still on this ALB") is what
   `--verify` fails on, and it's the check that would have caught rev 1.
5. **Ship a rollback lever that depends on nothing.** `--remove` needs no
   Terraform, no CI, no live probe — just kubectl. Emergency levers with
   dependencies fail when you need them.

## Test the decision logic, not just the syntax

The highest-value thing I did was extract the gate's boolean logic into a
standalone harness and run all 6 reachable states. **It found a real bug in my own
fix:** in the outage state, the draft would have hit the "deny already live →
re-assert to correct drift" branch and *entrenched an active outage*. The fix was
ordering the outage check before the drift-correction path.

`bash -n` would never have caught that. It's not a syntax error; it's a
state-machine error. For any script with more than ~2 interacting booleans, tabulate
the states:

| deny live | integration target | correct action |
|---|---|---|
| no | edge / unknown | skip (exit 0) |
| no | internal plane | apply |
| yes | internal plane | re-assert (safe) |
| yes | **edge** | **refuse + error** ← the bug |

## bash gotchas hit here

- **`set -e` + trailing test in a function is a trap.**
  `[ -z "$x" ] && return 0` as the *last* command makes the function return
  non-zero when the test is false, which reads as an error. Write explicit
  `if ... then return 0; fi`. But note `[ "$V" = "None" ] && V=""` mid-script is
  **safe** — bash exempts non-final commands in an `&&` list from `set -e`. I
  verified this empirically rather than guessing:
  ```bash
  bash -c 'set -euo pipefail; V=real; [ "$V" = "None" ] && V=""; echo survived'  # survives
  ```
- **A list-typed `TF_VAR_*` must never be the empty string.** Terraform parses
  `TF_VAR_*` as HCL and `""` is not a valid list — `unset` it so the module
  default `[]` applies. GitHub Actions `env:` interpolation of an empty step
  output produces exactly that empty string, so this needs an explicit guard.
- **Strategic-merge patches replace `spec.rules` wholesale** (no patch merge key
  on the list). The patch must restate the catch-all rule, which creates a
  sync obligation with `ingress.yaml` — documented in both files.

## Environment facts (unchanged from rev 1, still true)

- dev account `879318057152`, `us-east-1`.
- Agent runner has **read-only** cluster access: `kubectl get ingress -n
  adp-gateway` → Forbidden (SA `adp-agents:agent-scaledjob-sa`). Client-side
  `kubectl apply --dry-run=client` **still contacts the API server** and fails on
  RBAC, so it is not an offline validator. Use a YAML parser instead (`pyyaml`
  was not preinstalled; `pip install pyyaml -q`).
- `terraform test` in `modules/gateway/infra/modules/api-gateway` needs
  `terraform init -backend=false` first; the 5 existing cases run in ~seconds.
- Pre-existing `terraform validate` warnings on main (deprecated `hash_key`,
  `kubernetes_service_account`) — unrelated, don't clean up in an unrelated PR.

## Process note

The reviewer offered two options (gate the deny, or fix the deploy flow to order
things). I implemented the **preferred** one and *also* fixed the deploy flow,
because gap 2 meant the preferred option alone would have left the deny's
precondition permanently unsatisfiable — the deny would simply never apply, and
the issue's actual security goal would silently not ship. Worth checking whether
a "safe" gate is safe-but-inert before calling it done.
