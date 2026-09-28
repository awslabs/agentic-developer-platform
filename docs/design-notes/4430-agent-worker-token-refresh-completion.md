# Design note — completing the agent-worker token-refresh fix (#4430)

**Status:** design note (spike output). Implementable by one developer in a single PR.
**Issue:** #4430 · **Failed prior fix:** PR #4382 · **Original:** #4369 · **Parent EPIC:** #768
**Prior art:** #1469 (token file for the SDK subprocess), #4272 (broker mode)

---

## 1. Summary

Runs longer than ~1 hour still die in a `401 Bad credentials` loop on the image that
shipped PR #4382. Two pods proved it the same afternoon (evidence in §2).

This note establishes that **one of the two gaps named in the issue is not what the
code actually does**, identifies the real frozen token, and confirms the second gap
with the precise mechanism that keeps the watchdog from ever firing.

| | Filed hypothesis | What the code actually does | Verdict |
|---|---|---|---|
| **Gap 1** | Subprocess reads a token captured at process start | Subprocess re-reads the token file on *every* git/gh call — already correct | ❌ **hypothesis wrong** |
| **Gap 1 (real)** | — | `CheckRunStreamer` captures `GITHUB_TOKEN` once at construction and sends it on every PATCH for the life of the run | ✅ **root cause A** |
| **Gap 2** | Watchdog never observes subprocess 401s | Watchdog *does* observe them, but `reset()` on interleaved output means the streak never reaches the threshold; worker-side 401s are never routed to it at all | ✅ **root cause B** (mechanism differs) |

The correction matters: implementing Gap 1 as filed — "make the subprocess re-read
the token file" — is a **no-op**, because that is already the behaviour. A PR built to
the hypothesis would ship, look plausible, and change nothing. This would be the
third attempt at the same bug.

---

## 2. Evidence → mechanism

Both pods ran the fixed image and logged `Token manager initialized with 5-minute
refresh interval`.

| Pod | 401s | Terminal state | Explained by |
|-----|------|----------------|--------------|
| `agent-scaledjob-65wjt-rdgxd` (#4214) | 27× over ~67 min | `PATCH failed 66/150`, stuck turn 175 | **A** — `PATCH failed` is `checkRunStreamer.ts:530`, verbatim. Direct fingerprint of the frozen streamer token. |
| `agent-scaledjob-vlkmg-hnwqt` (#4251) | 36× over ~27 min | hung on `gh pr checks`, turn 179 | **B** — 401s seen but never escalated; no abort, so the run hung instead of redelivering. |

Neither pod exited. Neither logged `forcing token refresh` or `aborting for retry`
(`agent-worker.ts:1366,1381`). The watchdog was installed and silent — consistent
with a counter that never reached its threshold, not with a missing watchdog.

---

## 3. Root cause A — the streamer's token is frozen for the run

### Trace

```
agent-worker.ts:1293   const crToken = process.env.GITHUB_TOKEN || '';   // read ONCE, pre-query
agent-worker.ts:1299       token: crToken,                              // passed by value
checkRunStreamer.ts:134    this.cfg = cfg;                              // stored
checkRunStreamer.ts:547    Authorization: `Bearer ${this.cfg.token}`    // every PATCH, forever
```

`cfg.token` is never reassigned and the class exposes no setter (verified by grep for
`cfg.token` / `setToken` / `updateToken` in `checkRunStreamer.ts`). The streamer
PATCHes every ~2s decaying to ~60s (`MAX_PATCHES = 150`, `checkRunStreamer.ts:66`).
Once the startup token passes ~60 min, **every remaining PATCH 401s** — regardless of
how many times the token manager re-mints, because the manager updates `process.env`
and the token file, and the streamer reads neither.

### Why #4382 missed it

#4382 correctly reasoned that "the writes that matter happen in the SDK subprocess"
(`authWatchdog.ts:6-11`) and hardened the file-based subprocess path. But the
check-run streamer is **worker-side** and holds the only long-lived by-value copy of
the token in the process. It was outside the frame.

### Contrast — the paths that are already correct

| Consumer | Token read | Fresh? |
|---|---|---|
| `git-askpass-helper:5-10` | `cat "$TOKEN_FILE"` per git call | ✅ |
| `gh-wrapper:16-20` | `cat "$TOKEN_FILE"` per gh call | ✅ |
| `ghPost.ts:153` | `process.env.*` read per call | ✅ |
| `github-comments.ts:367` | `process.env.*` read per call, `this.options.token` last | ✅ |
| **`checkRunStreamer.ts:547`** | **`this.cfg.token` captured at construction** | ❌ |

The repo already has a convention — *read the token at use time, never hold it*. The
streamer is the single violator. The fix is to bring it into line, not to invent a
new mechanism.

### Fix A — inject a provider, not a value

Change `CheckRunStreamerConfig.token: string` to a getter:

```ts
/**
 * Resolve the token AT PATCH TIME. Never a captured string: a run outlives its
 * installation token (~60 min), so a value captured at construction 401s for the
 * rest of the run while the token manager re-mints into env/file that this class
 * never re-reads (#4430).
 */
tokenProvider: () => string;
```

Call site (`agent-worker.ts:1299`):

```ts
tokenProvider: () => process.env.GH_APP_TOKEN || process.env.GH_TOKEN || process.env.GITHUB_TOKEN || '',
```

and `checkRunStreamer.ts:547` becomes `Bearer ${this.cfg.tokenProvider()}`.

Why a provider over `process.env` read inline: keeps the class testable without env
mutation, and mirrors the precedence ladder already used at `ghPost.ts:153`.

The construction-time guard at `agent-worker.ts:1295` (`crToken &&`) must become a
one-shot presence check — keep "don't start a streamer with no token at all", but do
not let that startup value become the token used for PATCHes.

---

## 4. Root cause B — the watchdog cannot reach its threshold

Two independent defects. **Both must be fixed**; either alone leaves the backstop dead.

### B1 — `reset()` on interleaved output destroys the streak

`authWatchdog.ts:90-98`: any observation that is not an auth failure calls `reset()`,
zeroing the counter. Escalation needs **3 consecutive** failures
(`authWatchdog.ts:46,102`).

In the real stream, observations arrive interleaved (`agent-worker.ts:1512` feeds
assistant `turnText`; `:1526,1541` feed tool_results):

```
tool_result: "401 Bad credentials"   → count 1
assistant:   "Let me retry that..."  → reset → count 0
tool_result: "401 Bad credentials"   → count 1
assistant:   "Hmm, trying again..."  → reset → count 0
```

Max streak = 1. Threshold 3 is unreachable. **27 and 36 real 401s produced zero
escalations** — precisely what the pods show.

The unit tests pass because they feed `FAIL` three times back-to-back
(`authWatchdog.test.ts:57-64`) — a sequence the SDK stream never produces. The test
encoded the same wrong assumption as the code, which is why the bug shipped green.
The `reset()` intent was sound (don't kill a healthy run on scattered blips,
`authWatchdog.test.ts:80`); the implementation conflated "recovered" with "emitted
any other text".

**Fix B1 — sliding time window instead of a consecutive streak.**

Replace the streak counter with a bounded timestamp list:

- `observe(text, now)` pushes a timestamp on auth failure; drops entries older than
  `windowMs` (default **10 min**).
- Escalate to `force_refresh` when the window holds ≥ `threshold` (default **3**).
- **A clean observation no longer clears history.** Instead, track the last
  *successful GitHub write*; only that evidences recovery. Non-auth prose is not
  proof of health — that conflation is the bug.
- Cap the list at `threshold` entries so it cannot grow on a long run.

This preserves the anti-flap intent (3 failures inside 10 min is a cluster, not a
blip; 3 failures spread over an hour with successes between them is not) while making
the interleaved case — the only case that occurs in production — escalate.

Keep `now` injected rather than calling `Date.now()` internally, so the fake-clock
test in §6 is possible. The class stays pure (`authWatchdog.ts:16-20`).

### B2 — worker-side 401s never reach the watchdog

`applyAuthWatchdog` is called from exactly three sites (`agent-worker.ts:1512,1526,1541`),
all inside the SDK message loop. The streamer's 401s never enter that loop —
`checkRunStreamer.ts:529-531` swallows them into `this.warn(...)` → `log('WARN', ...)`.

So the highest-signal 401s in the entire run — a PATCH failing every ~60s against a
known-dead token — are invisible to the watchdog. Fixing A alone would stop the
streamer 401ing but leave the backstop blind for any future frozen-token holder;
fixing B1 alone leaves the streamer's evidence unused.

**Fix B2 — route streamer PATCH failures into the watchdog.**

Add an optional `onPatchError?: (message: string) => void` to
`CheckRunStreamerConfig`, invoked at `checkRunStreamer.ts:530` alongside the existing
warn. Wire it at the call site to `applyAuthWatchdog`. `looksLikeAuthFailure`
(`authWatchdog.ts:56-69`) already matches `HTTP 401` — the exact string
`_doPatch` throws at `checkRunStreamer.ts:557` — so no matcher change is needed.

Keep it fail-soft: PATCH errors must never throw (`checkRunStreamer.ts:529`).

### B3 — bound the abort

Currently abort requires a second full cluster after `force_refresh`, with no time
bound (`authWatchdog.ts:106-114`), so "bounded time" is undefined — the issue asks
for a number.

**Define it:** once a `force_refresh` has occurred, if auth failures are still
arriving **5 minutes** later, return `abort`. Worst case from first 401 to
`EXIT_RETRYABLE`: ~15 min (≤10 min to fill the window + 5 min post-refresh
confirmation). Compare with today's unbounded hang (68 and 152 min observed).

Rationale: 5 min is several PATCH cycles and several agent turns — long enough that a
genuine re-mint would have taken effect, short enough to beat a turn-cap burn.

The existing abort path is already correct and needs no change: it records cause,
flushes, and exits `EXIT_RETRYABLE` (`agent-worker.ts:1380-1389`).

---

## 5. Redelivery semantics — verified, no change needed

The issue flags "abort without redelivery" as a worst-case (task lost). **Verified
safe:**

- `agent-worker.ts:145` — `EXIT_RETRYABLE = 75`
- `entrypoint.py:60` — `AGENT_EXIT_RETRYABLE = 75`
- `entrypoint.py:2050` — `return worker_exit_code != AGENT_EXIT_RETRYABLE` gates the
  ack-by-delete, so exit 75 leaves the message untouched; its visibility timeout
  lapses and SQS redelivers, bounded by `maxReceiveCount` → DLQ.

This is the automatic form of the manual `kubectl delete job` recovery. Both
constants are already comment-linked as "keep in sync" — **preserve that pairing**;
changing one alone converts every abort into a lost task.

---

## 6. Test plan

Each test must **fail on today's code**. A test that passes before the fix re-runs the
#4382 mistake, where green tests accompanied a broken production path.

### Fix A — streamer token rotates
1. **`checkRunStreamer.token-rotation.test.ts`** (new) — construct with
   `tokenProvider: () => currentToken`, mock `fetch`, PATCH, mutate `currentToken`,
   PATCH again; assert the second request's `Authorization` header carries the **new**
   token. *Fails today:* the captured string cannot change.
2. **Startup guard** — a streamer is still not constructed when no token exists at
   startup (`agent-worker.ts:1295` behaviour preserved).

### Fix B — watchdog escalates and aborts
3. **Interleaved 401s escalate** — alternate auth-failure and clean text, mirroring
   the production stream; assert `force_refresh` within the window. ***This is the
   central regression test*** — it reproduces exactly what both pods did and is the
   one test that would have caught #4382. *Fails today:* `reset()` zeroes the streak.
4. **Anti-flap preserved** — failures spread beyond `windowMs` with successful writes
   between them never escalate. Guards against over-correcting into premature aborts
   (the "watchdog too aggressive" row of the issue's blast-radius table).
5. **Bounded abort** — with a fake clock: fill the window → `force_refresh`; advance
   past the 5-min deadline with failures continuing → assert `abort`, and assert the
   worker path calls `process.exit(75)` (mocked) with
   `writeResultMetadata({auth_failure: true})`.
6. **Streamer 401s feed the watchdog** — assert `onPatchError` fires on a 401 PATCH
   and that its message satisfies `looksLikeAuthFailure`.

### Regression
7. Short (<1h) runs unaffected; token file written before the SDK query starts
   (`agent-worker.ts:1888`); `execWithFreshToken`'s retry-on-401
   (`token-refresh.ts:380-392`) intact.
8. Existing `authWatchdog.test.ts` cases 4/6 (`:80`, `:96`) must still pass —
   recovery must not latch toward abort. **Cases at `:50`/`:57` encode the
   consecutive-streak assumption and must be rewritten**, not deleted: the intent
   ("one 401 cannot kill a healthy run") stays; the mechanism changes.
9. `token-refresh-cadence.test.ts` and `token-file-on-local-mint.test.ts` unchanged.

### Smoke test (operator, post-deploy)
Dispatch a >70-min task; on a fresh pod confirm the new image and:

```
kubectl logs -n adp-agents <pod> | grep -c 'Bad credentials'   # expect 0
kubectl logs -n adp-agents <pod> | grep -c 'PATCH failed'      # expect 0
kubectl logs -n adp-agents <pod> | grep 'Token refreshed proactively'
```

The check-run page must keep updating past the 60-min mark — the direct
user-visible proof of Fix A — and the run must produce a PR.

---

## 7. Files to modify

| File | Change |
|---|---|
| `agent/src/components/checkRunStreamer.ts` | `token: string` → `tokenProvider: () => string`; use at `:547`; add `onPatchError` at `:530` |
| `agent/src/agent-worker.ts` | `:1293-1299` pass provider not value; wire `onPatchError` → `applyAuthWatchdog` |
| `agent/src/lib/authWatchdog.ts` | sliding window + injected clock + bounded post-refresh abort deadline |
| `agent/src/lib/authWatchdog.test.ts` | rewrite streak-based cases; add interleaved + bounded-abort |
| `agent/src/components/checkRunStreamer.token-rotation.test.ts` | **new** |

**Not in scope** (verified correct, do not touch): `git-askpass-helper`, `gh-wrapper`,
`lib/tokenFile.ts`, `token-refresh.ts` mint/write paths, `entrypoint.py`.

`agent-pm.ts` — the issue asks whether it shares the pattern. It does **not**
construct a `CheckRunStreamer`; it calls `setToken(GH_APP_TOKEN, 1h)`
(`agent-pm.ts:195`) and shares the token manager. Out of scope here; if its own
long-run behaviour needs review, file separately rather than widening this PR.

---

## 8. Deployment

- **Automatic on merge:** agent-runtime image rebuild; new dispatches pull it.
- **NOT triggered:** no `gateway-deploy.yml`, no webhook-ingress change, no Terraform.
  Worker-image only. In-flight runs on the old image are unaffected until they cycle.
- **Manual follow-up:** confirm KEDA launches the new image; watch one >70-min run.
- **Environment coverage:** dev on merge; prod via normal image promotion.
- **Rollback:** revert the PR and rebuild, or pin KEDA to the prior tag. Runtime-inert
  outside the token path. Note the failure mode is *unchanged from today* on rollback —
  long runs resume dying at 60 min.

**Cost/quota:** none new. Fix A adds no API calls (same PATCHes, valid token). Fixes B
add no calls except a `forceRefresh` that today's code already intends.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Window too aggressive → healthy runs abort | Recovery requires a successful write, not mere prose; anti-flap test #4; abort still needs a failed forced refresh first |
| Another frozen-by-value token holder exists | Fix B2 makes any future one *visible* to the watchdog instead of silent. Audited: streamer is the only current violator (§3) |
| `EXIT_RETRYABLE` drift between TS and Python | Both sides comment-linked; §5 regression check |
| Repeated abort → DLQ churn | Bounded by `maxReceiveCount`; abort writes `stop_reason: github_auth_401` for triage |

---

## 10. Verdict

⚠️ **Ready with caveats** — implementable in one PR by one developer.

Caveats the implementer must accept:
1. **Gap 1 as filed is not the bug.** Do not "make the subprocess re-read the token
   file" — it already does. The frozen token is `CheckRunStreamer`'s.
2. **Fix A and Fix B are both required.** A alone leaves the backstop blind to the
   next frozen-token holder; B alone leaves the streamer 401ing.
3. **Every new test must fail before the fix.** Test #3 (interleaved 401s) is the
   one that would have caught #4382.
4. Numbers chosen here (10-min window, 3 failures, 5-min abort deadline) are
   defaults to encode as named constants, not magic literals.
