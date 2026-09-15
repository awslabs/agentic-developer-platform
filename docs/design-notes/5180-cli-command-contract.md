# CLI command contract — ADP CLI uplift (#5180 / #5185)

**Status:** foundation implementation in PR #5188; domain integration in PR #5179. Release/live acceptance is recorded separately.
**Foundation story:** #5185 · **Epic:** #5180 · **Coordinate shared files with:** #5039, #5179

This document is the interface between the shared CLI foundation and the four
domain feature stories. It exists so four developers working in parallel can add
commands without editing each other's code, and without four different answers to
"where does the gateway URL come from?".

It is a **contract**, not a plugin framework. There is no registry, no discovery
by directory scan, no dynamic loading. A domain helper is one file, imported by
one line in a dispatch table. That is deliberate: a framework would be more code
than the four helpers it serves.

---

## 1. Command names

The everyday CLI is unchanged. Nothing is renamed, and no grouped duplicate of an
existing shortcut is added.

| Command | Owner | Status |
|---|---|---|
| `adp login`, `status`, `logout`, `token`, `refresh`, `import`, `serve` | #5185 | shipped, unchanged |
| `adp codex [args…]`, `adp codex setup` | — | shipped, **must not change** |
| `adp claude [args…]`, `adp claude setup` | — | shipped, unchanged |
| `adp daemon install\|uninstall`, `adp update [--rollback]`, `version`, `help` | #5185 | shipped, unchanged |
| `adp admin login` | #5185 | **new** — native Cognito bootstrap (§4) |
| `adp admin setup` | #5185 | **new** — resumable guided setup (§5) |
| `adp admin bedrock connect\|list\|verify\|status` | #5181 | canonical administrative surface |
| `adp bedrock status` | #5181 | caller’s effective route; legacy forms remain compatible |
| `adp aws <action>` | #5182 | |
| `adp admin github <action>` | #5183 | |
| `adp github <action>` | #5184 | |

### `adp codex` is frozen

`adp codex` claims exactly one token, `setup`. Everything else — including nothing
at all — is a launch with full argument forwarding, and `adp codex -- setup` is the
escape hatch for launching with a literal `setup` argument. Any change to that
dispatch is a regression, not a feature. The same rule applies to `adp claude`.

Feature stories add their command under the agreed area and dispatch it to their own file.
They do not add flags to, reorder, or reinterpret existing verbs.

---

## 2. Dispatch and the shared helper

### 2.1 File layout

All CLI files install side by side in one directory (`~/.adp/bin` by default), and
`adp` resolves its siblings by its own real path, never via `PATH`.

```
~/.adp/bin/
  adp                  # bash dispatcher (#5185)
  bg-cognito-auth.sh   # auth core: login/token/refresh/serve (#5185)
  bg-gateway-proxy.py  # Codex loopback proxy
  adp_common.py        # shared helper module — import this (#5185)
  adp-admin.py         # admin login + guided setup (#5185)
  adp-bedrock.py       # #5181
  adp-aws.py           # #5182
  adp-github.py        # #5184
  adp-github-admin.py  # #5183
```

Python helpers are **standard library only**. A CLI that needs `pip install` on a
laptop is a CLI that does not get installed. `adp_common.py` is imported as a
sibling module; helpers add their own directory to `sys.path` via the two-line
preamble in §2.3.

### 2.2 Registering a command (the whole integration surface)

A feature story's patch to shared files is exactly three edits, and nothing else:

1. **`adp`** — one `case` arm delegating to the helper:
   ```bash
   bedrock) exec_python_helper "adp-bedrock.py" "$@" ;;
   ```
   `exec_python_helper` is provided by the foundation: it checks `python3` is
   present, checks the helper file exists (pointing at `adp update` if not), and
   `exec`s it so the helper owns the terminal and the exit status.
2. **`adp` usage text** — one entry in the matching help section.
3. **`install.sh` + the download allowlist** — the helper's filename added to
   `CLI_FILES` (install.sh) and `ALLOWED_SCRIPTS` (`src/cli_download/routes.py`).

The integration owner (#5185) applies these sequentially. Feature PRs keep them
minimal so the merges do not conflict. **A file enters the allowlist and the help
text in the same PR that ships its implementation** — never before, or the CLI
advertises a command that 404s on download.

### 2.3 Helper preamble

Every Python helper starts with this, verbatim:

```python
#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common  # noqa: E402
```

---

## 3. Transport contract (`adp_common.py`)

### 3.1 Gateway and organization resolution

```python
gateway_url() -> str
```
Returns the base URL from `~/.bedrock-gateway/config.json` key `gateway_url`,
canonically ending in `/api`. Raises `CliError` with the reinstall line if absent.
It is **never guessed** from the environment — a wrong origin silently points the
CLI at somebody else's deployment. The URL must be HTTPS, or HTTP on loopback
only; a URL carrying userinfo, a query or a fragment is rejected before any
credential is sent.

Organization scope resolution order, for any command taking `--org`:
`--org` flag → `ADP_ORG` env → the org claim on the current session → the
gateway's default for the caller. `--org` is the ADP organization; where a command
also needs an external GitHub app owner, that is a **separate** flag
(`--github-org`) and the help must say which is which.

### 3.2 Authenticated requests

```python
api(method, path, body=None, *, timeout=120) -> dict
```

- Acquires a bearer token by shelling out to `bg-cognito-auth.sh token`. That is
  the single implementation of refresh-on-expiry and of the cross-process refresh
  lock; helpers must not read `tokens.json` themselves, or two commands running
  at once will race the refresh.
- Redirects are refused, not followed: a redirect before a credential is sent is
  a misconfigured gateway, not a route.
- Tokens are held in memory for the duration of one call. They are never written
  to a file by a helper, never placed in argv, and never logged.

### 3.3 HTTP error mapping

`api()` raises `CliError` with a message safe to print. No response body is echoed
verbatim — bodies can contain setup parameters. Only a short `[a-z_]` reason code
is surfaced, if present.

| Status | Meaning | Message must say |
|---|---|---|
| 401 | session dead | run `adp login` (or `adp admin login`) |
| 403 | authenticated but not authorized | which authority is required |
| 404 | unknown target, or gateway too old | check the target; consider upgrading the gateway |
| 409 | already exists / already decided | the existing state and how to reuse it |
| 429 | rate limited | wait and retry |
| 5xx, timeout, DNS | outcome uncertain | check current state before retrying; a mutation may have completed |

A failed mutation must state whether anything changed. "Retry the same command to
resume" is only correct for operations that are genuinely idempotent (§3.6).

### 3.4 Exit codes

| Code | Meaning |
|---|---|
| 0 | success, or a `--dry-run`/status read that completed |
| 1 | usage error (unknown flag, missing required argument) |
| 2 | authentication required or expired |
| 3 | authenticated but not authorized |
| 4 | a pending external action blocks completion (approval, administrator handoff) |
| 5 | the operation failed and did not complete |

Exit 4 is not a failure: it is the resumable state. Scripts distinguish "waiting
on a human" from "broken" by that code.

### 3.5 Output: `--json`, `--dry-run`, `--yes`

`--json` prints exactly one JSON object on stdout and nothing else; all human
narration goes to stderr. The envelope is fixed:

```json
{
  "status": "configured|verified|pending|failed|unavailable",
  "command": "bedrock connect",
  "detail": {},
  "next_action": "human-readable next step, or null",
  "error": {"code": "snake_case", "message": "safe text"}
}
```

`error` is present only for `failed`. `detail` is command-specific and must never
carry a password, private key, OAuth secret, webhook secret or token — machine
output is the easiest thing to accidentally log.

`--dry-run` prints what would change and exits 0 without mutating. `--yes`
suppresses interactive confirmation only; it never suppresses a required
credential prompt.

### 3.6 Private resumable state

```python
state_path(name) -> Path      # ~/.adp/state/<name>.json
read_state(name) -> dict
write_state(name, obj) -> None
```

`~/.adp/state/` is created `0700`, files written `0600` via write-then-rename so a
reader never sees a half-written file. Each provider owns exactly one state file
and does not read another's. State holds progress and identifiers so a retry
resumes; it must not hold credentials.

State is a **hint, not proof**. Configuration recorded in state does not establish
that sign-in works, that a repository is reachable, or that a model can be
invoked. A provider that reports `verified` must have made a call that proves it.

### 3.7 Browser continuation

```python
open_browser(url) -> bool
```
Prints the URL first, then attempts to open it, and returns whether that
succeeded. The URL is always printed, because a headless machine or an SSH
session must still be completable by hand. A flow that requires a browser exits 4
with the URL and the resume command when no browser is available — it never hangs
waiting for a callback that cannot arrive.

---

## 4. Native administrator bootstrap (`adp admin login`)

The problem this solves: on a fresh deployment, the first administrator has a
native Cognito username and password and no GitHub App yet. The existing
`adp login` cannot serve them — it mints tokens by resetting the user's password
(safe only for broker-provisioned `github_*` users, who hold none) and it needs a
browser session that does not exist yet.

### 4.1 Mechanism

```
CLI  ── POST /api/auth/cli/password  {username, password}
     ◄─ tokens, or {challenge, continuation}
CLI  ── POST /api/auth/cli/challenge {continuation, responses{…}}
     ◄─ tokens, or the next challenge
```

- The routes live in `src/auth/cli_native_login.py`; existing browser login/refresh remain in `cli_login.py`. The gateway calls `ADMIN_USER_PASSWORD_AUTH` on the **CLI app client** and
  `AdminRespondToAuthChallenge` for continuations. It **never** calls
  `AdminSetUserPassword` — a native user's password is changed only inside the
  `NEW_PASSWORD_REQUIRED` challenge they were actually asked to complete.
- Supported challenges: `NEW_PASSWORD_REQUIRED`, `SMS_MFA`, `SOFTWARE_TOKEN_MFA`.
- Enrollment challenges the CLI cannot serve (e.g. `MFA_SETUP`) are reported with
  the required action. MFA is never disabled or bypassed to get past them.
- The CLI reads username at a normal prompt and every secret with echo off. No
  credential or challenge value appears in argv, in shell history, or in any log
  on either side.

### 4.2 Continuation binding

`continuation` is an opaque HMAC-signed envelope minted by the gateway, carrying
the Cognito session, the username, the challenge name, the pool id and the client
id, with a short expiry and a single-use marker. The gateway verifies the
signature and that the presented responses match the challenge the envelope was
issued for. A tampered, replayed, expired or cross-flow envelope is rejected —
the client cannot substitute a different user, a different challenge or a
different pool. Failures are rate-limited per IP and per username through expiring database records (serialized per key with PostgreSQL transaction locks), and every
failure returns the same generic message so the endpoint does not disclose
whether an account exists.

### 4.3 After success

Tokens are written in the **existing** session format
(`~/.bedrock-gateway/tokens.json` + `config.json` with `refresh_via=gateway`), so
`adp token`, `adp refresh`, `adp codex` and `adp claude` all work afterwards with
no changes. Platform-admin authority is then confirmed **server-side**; the CLI
does not decide it from a token claim. An expired session, a non-admin user or a
denied challenge cannot mutate setup.

---

## 5. Provider contract for guided setup (`adp admin setup`)

`adp admin setup` orchestrates. It contains **no domain logic** — no GitHub call,
no AWS call, no Bedrock call. Every step is a provider owned by a feature story.

### 5.1 Interface

A provider is a Python module exposing:

```python
NAME = "github"                 # stable slug; also the state file name
TITLE = "GitHub App"            # shown to humans
ORDER = 20                      # ascending; foundation reserves < 10

def status(ctx) -> dict: ...     # read-only; no mutation, no prompting
def configure(ctx) -> dict: ...  # may prompt and mutate; must be resumable
```

Both return the §3.5 envelope shape:

```python
{"status": "configured"|"verified"|"pending"|"failed"|"unavailable",
 "detail": {...}, "next_action": "..." or None,
 "error": {"code": "...", "message": "..."} or None}
```

| Status | Means |
|---|---|
| `verified` | proved working by a real call just now |
| `configured` | configuration present, not proved working |
| `pending` | blocked on an external human action; `next_action` says which |
| `failed` | attempted and did not succeed; `error` explains, `next_action` says what to try |
| `unavailable` | this deployment/CLI build cannot do it (helper not installed, gateway too old) |

`unavailable` is what keeps the wizard honest. A provider whose helper is not
installed is reported as unavailable with the reason — never silently skipped, and
never reported as done.

### 5.2 Orchestration rules

1. Authentication first. The wizard checks for a live session; if there is none it
   offers `adp admin login` (native) or `adp login` (browser) and resumes after.
2. `status()` on every provider, in `ORDER`, before any mutation. The user sees the
   whole board before answering a single question.
3. `configure()` only on providers that are `pending`, asking only for what
   is missing, and offering reuse of existing configuration where the provider
   reports it.
4. A `pending` provider does not block the others. The wizard continues and prints
   the pending items and their next actions at the end.
5. The wizard exits 0 when everything is `verified`/`configured`, 4 when anything
   is `pending`/`unavailable`, 5 when anything is `failed`. A successful read-only
   `--dry-run` exits 0 even when setup remains pending.
6. The wizard never writes another provider's state file.

### 5.3 Registration

One line in `adp-admin.py`'s provider list. The module is imported lazily inside a
`try/except ImportError`, so a missing helper becomes `unavailable` rather than a
traceback.

---

## 6. Packaging, discovery and deployment

- **Install/update/rollback** cover every file in the layout of §2.1. `install.sh`
  stages all files and commits them only when all have downloaded, so a partial
  download never leaves a working `adp` beside a missing helper. `adp update`
  keeps `*.prev` copies and `--rollback` restores every one of them.
- **Discovery works before sign-in.** `GET /api/cli/{name}` is public by design,
  and the unauthenticated login page shows the install command with the
  deployment's real origin filled in. Installation persists that URL once, so the
  next command needs no flag. The installer prints an absolute-path invocation so
  the very next command works before the shell's `PATH` is reloaded.
- **Both gateway-deploy filters must cover `modules/gateway/cli/**`** — the push
  `paths:` list and the backend change detector. Until #5185 they covered neither,
  so a CLI-only merge rebuilt nothing and the served artifact silently stayed on
  the old revision. A merge is not evidence of delivery; the served file is.

---

## 7. Security invariants (all stories)

1. No password, private key, OAuth secret, webhook secret or token in argv, in
   logs, in `--json` output, or in a state file.
2. Local AWS credentials stay local. The gateway is never sent one.
3. Never reset a native Cognito password to obtain a token. Never alter role or
   group grants as a side effect of authenticating.
4. Authorization is decided by the server. A CLI-side claim check is a UX
   nicety, never the control.
5. Retries must not create duplicate destinations, connections, apps or mappings.
   Idempotency is keyed on a stable identifier, not on local state alone.
6. Configuration presence is not proof of function. `verified` requires a call.

---

## 8. Ownership

| Area | Owner |
|---|---|
| `adp` dispatcher + help, `install.sh`, `bg-cognito-auth.sh`, `adp_common.py`, `adp-admin.py` | #5185 |
| `src/auth/cli_login.py`, `src/cli_download/routes.py`, deploy filters, shared fixtures, login-page discovery | #5185 |
| `adp-bedrock.py` + Bedrock routing backend | #5181 |
| `adp-aws.py` + `src/auth/aws_connect_routes.py` | #5182 |
| GitHub admin helper + app register/manual/status handlers | #5183 |
| GitHub user helper + install/callback/repository handlers | #5184 |

Feature stories touch shared files only for the three registrations in §2.2. The
integration owner reviews and merges those sequentially. Only the integration
owner edits the central CLI overview (`modules/gateway/cli/README.md`); feature
docs live in separate per-area files.

## Scripted administrator login

Use `adp admin login --credentials-file /private/path/login.json --json` (file mode
0600), or `--credentials-stdin` with a JSON object from a secret manager. Keys are
`username`, `password`, and when challenged `new_password`, `sms_mfa_code` or
`software_token_mfa_code`. Required new-user attributes use `userAttributes.name`
keys named by the server. No secret values belong in command arguments. A failed
MFA attempt consumes its continuation; restart login with a fresh code. Native
login verifies `/auth/cli/admin-session` before saving tokens, using the same
refresh lock and session format as the established CLI.
