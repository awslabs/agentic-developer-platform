# CLI onboarding eval (clean room)

End-to-end evaluation of the journey a new developer actually walks: sign in,
get approved, download the helper, point Claude Code (or Codex) at the gateway,
get a completion. It runs nightly against dev and can be triggered on demand.

Issue #4157 — Story 5 of EPIC #4143. It automates the manual live validation
performed on 2026-08-26, so the journey shipped by #4144 (approval gate), #4145
(CLI seeding + discovery), #4146 (setup-page download route) and #4154 (Codex
zero-touch proxy) is re-proven continuously instead of once, by hand.

| | |
|---|---|
| Workflow | [`.github/workflows/eval-cli-onboarding.yml`](../../../.github/workflows/eval-cli-onboarding.yml) |
| Script | [`run-eval.sh`](run-eval.sh) |
| Harness tests | [`tests/test-run-eval-dry-run.sh`](tests/test-run-eval-dry-run.sh) |
| Schedule | 05:00 UTC nightly, plus `workflow_dispatch` |

## Why "clean room" is in the name

The eval must not run in the agent-worker image, or in any environment where a
CLI is already pointed somewhere. That image ships `~/.codex/config.toml`
aimed at a sigv4-proxy on `127.0.0.1:9090`, Claude Code settings, `ANTHROPIC_*`
env and platform IRSA. Any one of them silently substitutes the platform's own
internal auth for the auth path under test — the eval would go green while
every request rode the agent's credentials rather than the user's.

**A false green here is worse than having no eval**, because it actively
certifies a journey nobody checked. Two mechanisms make the boundary real:

1. **`--assert-clean-room`** is the workflow's first step. It fails the job if
   `~/.codex`, `~/.claude`, `~/.bedrock-gateway` or `~/.claude.json` exists, if
   any `ANTHROPIC_*` / `ADP_GATEWAY_*` / `CLAUDE_CODE_*` variable is set, if
   `claude` or `codex` is already on `PATH`, or if anything is listening on the
   proxy ports. The job runs in a stock `node:20-bookworm` container with a
   fresh `$HOME` so this check can pass honestly.
2. **`laptop()`** wraps every command that emulates the developer. It strips
   every AWS credential variable and disables IMDS, so a laptop step *cannot*
   borrow the runner's IRSA even by accident. Only `h_aws`, `h_kubectl` and
   `h_psql` — grep for them — ever touch credentials.

That split is what makes the whole thing possible: `cognito-idp:InitiateAuth` is
an **unsigned** API, so `import` / `token` / `refresh` genuinely need no AWS
credentials. If a laptop step ever started requiring them, it would fail — which
is precisely the signal we want.

## What it covers

| Phase | Proves |
|-------|--------|
| **0** | Config resolves from SSM; the flag's starting value is **read before anything is mutated** |
| **A** | With the gate off, an un-approved user is *not* blocked — the baseline a Phase-B 409 is measured against |
| **B1** | With the gate on, all three enforced inference paths return the gate's own 409: `/v1/messages`, `/v1/chat/completions`, `/openai/v1/responses` |
| **B2** | `/auth/me` and `/access/status` stay reachable while gated — a pending user must still be able to see the "request access" screen |
| **B3** | The **DB-fallback** leg: approval written only to Postgres admits a token whose `org_id` claim is still blank |
| **B4** | An admin with a blank org is exempt — the person who approves everyone is never the first locked out |
| **C5–C6** | The helper downloads from the **live** route and `/.well-known/cognito-config` agrees with SSM |
| **C7–C8** | `import` (refresh token via stdin), `0700`/`0600` permissions, `token` prints a JWT, and that JWT buys a real completion |
| **C9** | Claude Code works configured exactly as the setup page renders it — confirmed by a `usage_logs` row for this user's sub |
| **C10** | Codex works through the local auth proxy (skipped until #4156 — see below) |
| **D** | The flag is restored to its start value and every throwaway resource is deleted, `if: always()` |

Assertions check **bodies, not just status codes**. The 409 must carry
`{"detail":{"error":"user_not_assigned_to_org","message":...}}` with exactly
that key set — a 409 from some other layer, or with a renamed field, is a
regression a status-only check would wave through.

### Two details worth knowing

**B3 cannot fool itself.** The gate has a fast path (a non-empty `org_id` claim)
and a DB fallback (a Postgres read keyed on `cognito_sub`). The eval never sets
`custom:org_id` on the Cognito user, because the pre-token-generation Lambda
copies user attributes into the access token and strips empty ones — so the
claim stays blank and only the DB read can admit the request. B3 additionally
re-reads the attribute to assert that premise, so a fast-path pass can never
masquerade as a DB-fallback pass.

**C5b/C10 are a soft skip, not a silent one.** `ALLOWED_SCRIPTS` in
`src/cli_download/routes.py` currently lists only `bg-cognito-auth.sh`, so
`bg-gateway-proxy.py` 404s and the Codex leg is skipped with a `⚪` in the
summary naming #4156. When #4156 lands, the branch flips to a pass with **no
edit here**.

## Out of scope: Tier 2

Identity is seeded at the **Cognito layer** (`admin-create-user` +
`admin-set-user-password` + `USER_PASSWORD_AUTH`), which yields exactly what the
SPA holds after a GitHub login: an access token and a refresh token. Everything
downstream of that is identical to the real thing.

The **real GitHub-OAuth browser leg is deliberately not covered.** It needs a
bot GitHub account and its TOTP secret in Secrets Manager, which is a separate
piece of infrastructure with its own security review — tracked in **#4166**.
Until then, note the gap honestly: a break in the OAuth broker itself would
not be caught here.

## Running it

```bash
# Nightly-equivalent full run (needs IRSA + kubeconfig for the target env)
gh workflow run eval-cli-onboarding.yml -f environment=dev

# A subset
gh workflow run eval-cli-onboarding.yml -f environment=dev -f phases=A,B

# Prove the eval still fails when the thing it tests is broken
gh workflow run eval-cli-onboarding.yml -f inject_failure=wrong-org
```

Locally, `--dry-run` stubs every external CLI, so it needs no AWS, no cluster
and no network:

```bash
./platform/evals/cli-onboarding/run-eval.sh --dry-run
./platform/evals/cli-onboarding/run-eval.sh --dry-run --fail-phase B  # cleanup-on-failure
./platform/evals/cli-onboarding/tests/test-run-eval-dry-run.sh        # the harness tests
```

The dry-run stubs re-implement the middleware's exemption order rather than
being told the expected answer per phase. That is deliberate: a stub handed the
right answer can never fail, so `--dry-run --inject-failure wrong-org` would
pass a broken run and the acceptance check would be worthless. The harness tests
assert both directions — injected fails, control passes.

## What it changes in the target environment, and how it cleans up

The eval **mutates dev**. Everything it touches is restored or deleted in Phase
D, which runs from an `EXIT` trap *and* as a separate `if: always()` step:

| Mutation | Restored by |
|----------|-------------|
| `BG_ENFORCE_ORG_ASSIGNMENT` on the gateway deployment | Set back to the value **read at start**. If there was no deployment-level override, the override is *removed* rather than pinned, so the deployment keeps tracking the configmap. |
| Two throwaway Cognito users (`eval-cli-onboarding*`) | `admin-delete-user` |
| One or two `users` rows | `DELETE` by the ids recorded in state |
| A local auth proxy process | Killed |
| The emulated laptop `$HOME`, tokens, curl configs | `rm -rf` |

Restoring to the value read at start — never a hardcoded `false` — is the point:
an eval that assumed `false` would silently disable the approval gate in an
environment where someone had turned it on. There is a test for that.

Only one live run is allowed at a time (`concurrency`), because two would fight
over the same flag and each would "restore" the other's mutation.

### If a run is killed mid-flight

Re-run `--cleanup-only`, which is idempotent and works standalone:

```bash
./platform/evals/cli-onboarding/run-eval.sh --environment dev --cleanup-only
```

Then check the flag and sweep any leaked users. Anything in Cognito matching
`eval-cli-onboarding*` is a throwaway from a crashed run and is safe to delete —
that prefix is the sweep convention.

```bash
kubectl get deploy bedrockgateway -n adp-gateway \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="BG_ENFORCE_ORG_ASSIGNMENT")].value}'
```

## Triaging a failure

The job summary carries a per-phase table (`✅` / `❌` / `⚪`) — read it first;
it names the failing assertion and therefore the surface:

- **Phase A fails** → the gateway is broken for everyone, not a gate problem.
  Check pods and `/api/health` before looking at the gate.
- **B1 fails on one path only** → a path-registry regression. `/openai/v1/responses`
  has slipped enforcement twice (#2792, #2809); check `src/shared/enforced_paths.py`.
- **B2 fails** → the gate leaked onto non-spend paths; pending users can no
  longer reach the screen that tells them to ask for approval.
- **B3 fails** → the DB fallback. Either the `users` lookup by `cognito_sub`
  broke, or the fast path is masking it (check the B3 premise assertion).
- **B4 fails** → the admin exemption; this is the self-lockout class (#3984).
- **C5/C6 fails** → the download route or the discovery document, i.e. the
  onboarding page hands out something that does not work.
- **C7/C8 fails** → the CLI helper itself.
- **C9/C10 fails** → CLI wiring. If C9's completion succeeded but the
  `usage_logs` assertion failed, traffic did *not* transit the gateway on this
  identity — treat that as more serious than a failed completion.
- **Phase D reports a restore failure** → act immediately. The environment may
  be left mis-flagged; the log includes the exact `kubectl set env` to fix it.

Nightly failures update a **single** tracking issue labelled
`cli-onboarding-eval-failure` rather than filing a fresh one each night, so the
label keeps meaning something.

## Known operational prerequisites

- The runner needs `cognito-idp:Admin*` on the target pool, RDS reachability,
  and `kubectl` access to `deploy/bedrockgateway`. The `runner-iam` module
  currently grants `cognito-idp:*`.
- The `eval` job uses a `container:`, which requires the ARC scale set to
  support container jobs. No other workflow in this repo uses `container:` yet,
  so this is unproven on our runners — tracked in **#4167**. The eval will not
  fall back to the host runner: a run outside the clean room would report a
  false green, which is the one outcome this eval exists to prevent.
- The eval spends a small amount of real inference on each run (a handful of
  short completions).
