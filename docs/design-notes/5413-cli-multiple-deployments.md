# Multiple ADP deployments from one installed CLI (#5413)

**Status:** implementation in the PR that references #5413. Live acceptance
(AC-10/AC-11) is recorded separately and is **not** satisfied by this change.
**Story:** #5413 · **Builds on:** #5180 / #5185 (the [CLI command
contract](5180-cli-command-contract.md)) · **Coordinate shared files with:** #5338

This note explains the design behind named deployments: what problem it solves,
the one rule that decides which deployment a command runs against, and why the
storage layout is shaped the way it is. The command contract in #5180 still
governs dispatch, transport and security; this is the layer underneath it that
answers "which gateway, and whose session?".

---

## 1. The problem

One person needs three ADP deployments at the same time — development,
integration, pre-production — in three terminals, with two different agents
(Codex and Claude) attached. Before this change there was exactly one deployment
per machine and exactly one session, because both halves of the CLI hardcoded
`~/.bedrock-gateway`:

- the bash half (`adp`, `bg-cognito-auth.sh`) for the config and token store,
- the python half (`adp_common.py` and the per-area helpers) for the same.

So "point at the other deployment" meant re-running `adp login` against a
different URL, which **overwrote** the first deployment's session. Three
terminals could not be signed in at once, and the Codex loopback proxy bound a
single fixed port, so the second terminal's `adp codex` either failed to bind or
silently attached to the first terminal's proxy — sending one deployment's
traffic, with one deployment's token, to the other deployment's gateway.

The failure mode that matters here is not a confusing message. It is a
**credential crossing a deployment boundary**. Every decision below is made
against that.

---

## 2. One implementation of the rule, called by both halves

The obvious approach — teach the bash half its precedence rules and the python
half its own — is a bug factory, because the two halves would drift and the
symptom of drift is the crossing above. So there is exactly one module,
`modules/gateway/cli/adp_deployments.py`, that owns:

- what a valid deployment name and URL are,
- where a deployment's private files live,
- and which deployment a command is running against.

Both halves call it, and neither re-derives the rule:

| Half | How it calls |
|---|---|
| bash (`adp`) | `adp_deployments.py resolve --format env` **once**, at entry, and `eval`s the exports |
| python (`adp_common.py`, area helpers) | `import adp_deployments` and call `resolve()` |

The module is stdlib-only and imports nothing from `adp_common`, because the bash
front door executes it directly and must not depend on the rest of the python
surface being loadable. It installs as a sibling in `~/.adp/bin` like every other
CLI file and is resolved by real path, never via `PATH` (§2.1 of the command
contract).

### Resolve once, then pin

`resolve_deployment()` in `adp` runs at entry, before the verb is dispatched, and
exports the result:

```
ADP_DEPLOYMENT_ID     the stable id — the authority
ADP_DEPLOYMENT_NAME   for messages
ADP_DEPLOYMENT_SOURCE why this one was selected
ADP_DEPLOYMENT_URL    the endpoint
BG_CONFIG_DIR         config.json + tokens.json         (the auth core's own variable)
ADP_STATE_DIR         resumable area state
ADP_RUNTIME_DIR       proxy port/identity + locks
ADP_LOG_DIR           proxy.log
BG_AWS_PROFILE        the AWS profile the Identity Pool exchange writes
```

Resolving once is not an optimisation. A command is a tree of processes — `adp`
spawns the auth core, which spawns the proxy, which is what Codex talks to — and
a concurrent `adp deployment use` in **another terminal** can change the saved
default between two of those steps. Re-resolving per process would let a command
combine one deployment's endpoint with another's token, without anything failing.
So children inherit the pin and honour it, and `ADP_DEPLOYMENT_ID` is the
authority: the path variables are *derived* from it and re-derived by `resolve()`,
so an inherited path variable on its own can never aim a helper at another
deployment's tokens.

### Selection precedence

First match wins:

1. an explicit `--deployment NAME` **before the verb** — this one command only
2. an already-resolved parent context (`ADP_DEPLOYMENT_ID`) — the pin above
3. a non-empty `ADP_DEPLOYMENT` environment variable — this terminal
4. the saved default (`adp deployment use NAME`)
5. the legacy implicit deployment — the pre-#5413 `~/.bedrock-gateway` store

An **unknown explicit selection fails**. It never falls back to another
deployment, because falling back is precisely how a credential reaches an
environment the user did not name. The same applies to `ADP_DEPLOYMENT`: naming
something unregistered is an error, not a hint.

`--deployment` is a global option and must appear before the verb, so that a verb
which forwards all of its arguments (`adp codex …`, which is frozen by the
command contract) is unaffected.

### Three verbs take no selection

`deployment`, `help` and `version` are resolved *past*, not resolved. They are
what you run when the selection is broken: `adp deployment list` has to work when
the saved default names something that no longer exists, and `adp help` must work
on a machine with nothing registered at all. `adp --deployment X deployment list`
is therefore **refused** rather than honoured — `adp deployment` manages
deployments and does not operate *on* one. To inspect the registry as a
particular terminal sees it, select through the environment:

```bash
ADP_DEPLOYMENT=integration adp deployment list
```

which reports `selection_source: environment`.

---

## 3. Commands

```
adp deployment add <name> --url <url>   register locally — no sign-in, no request
adp deployment list                     show records and which one is selected
adp deployment use <name>               change the saved default
adp deployment remove <name>            forget locally — never touches the cloud
```

`add` **makes no network call**. It writes a record, and nothing else. A
registered-but-not-signed-in deployment is therefore an ordinary state, not an
edge case, and everything that reads a deployment's URL has to cope with there
being no `config.json` yet. (`adp update` did not, which is one of the fixes in
this change — see §7.)

`add` has three deliberate outcomes:

| Case | Outcome |
|---|---|
| same name, same URL | unchanged — idempotent, re-running is safe |
| same name, different URL | **refused** — a name is never silently rebound |
| new name, existing URL | **alias**: same stable id, same session |

Silent rebinding is refused for the §1 reason: the token already in that store
was issued by the old gateway. An alias, conversely, is the *right* answer for
two names over one URL — one canonical URL, one id, one session. Copying a
rotating refresh token into two caches means whichever copy is used second is
already dead.

`remove` refuses the saved default (removing it would leave the next command with
no target) and refuses a deployment in active use. "In use" is established by
matching the recorded PID and process start time, plus the proxy's deployment
metadata. A stale file whose PID has been reused by an unrelated process does not
block removal or direct the user to terminate that process.

---

## 4. Storage layout

```
~/.adp/deployments.json                   registry: schema version, default, records (0600)
~/.adp/deployments/<stable-id>/
    config.json                           gateway URL + Cognito metadata
    tokens.json                           this deployment's session
    state/                                AWS, Bedrock, GitHub, admin area state
    runtime/                              proxy port/identity, spawn + refresh locks
    logs/                                 proxy.log
```

**The filesystem authority is a random stable id, not the name.** A name is a
user-facing label and can be removed and re-added pointing somewhere else; if the
directory were keyed on the name, an old process still holding that path would
begin writing into the new target's store. A random id makes that physically
impossible: a rebound name gets a different directory.

Every directory is created 0700 and every file 0600, via one `private_directory`
helper that also verifies ownership and mode after creation rather than trusting
`mkdir`'s mode against the process umask.

The registry is read-modify-written under a brief `mkdir`-based lock
(`~/.adp/registry.lock`). `mkdir` rather than `flock(1)` because macOS does not
ship `flock`. The lock is held only across the registry replacement itself —
never across a login, a network call or a token refresh, since those take minutes
and blocking every other terminal's `deployment list` behind one browser approval
would be its own bug.

### The legacy store is adopted in place

An existing single-deployment store is registered as the record named `default`,
with `legacy: true`, and its paths are **left where they are** — config and
runtime in `~/.bedrock-gateway`, state in `~/.adp/state`. Nothing is moved and no
token is copied, so a user who only ever wanted one deployment needs no new login
and no new commands. A home with a legacy store and three named deployments
therefore lists **four** records.

Two consequences worth stating because they look like inconsistencies:

- **Runtime files stay in the legacy directory.** `proxy.pid` and
  `proxy-spawn.lock` are how a *running* proxy is discovered. Relocating them
  during an upgrade would make an already-running proxy invisible to the new CLI,
  which would then try to start a second one on the same port and fail with an
  opaque "address already in use" — the upgrade breaking the thing it promised to
  leave alone.
- **0700 is required of a directory we create, not of one we inherit.**
  `install.sh` and the auth core have always created `~/.bedrock-gateway` with a
  plain `mkdir -p`, so on a normal umask it is 0755 on real machines. Demanding
  0700 there would fail every pre-existing user's first command after upgrading,
  over a mode they never chose — a regression, not a security win, since the
  token file itself is written 0600. What *is* still refused is a directory we do
  not own.

Adoption is also **read-only until a mutating command runs**: `status` and
`deployment list` resolve through a synthesized view that includes the legacy
store without writing anything, so inspecting a machine never changes it.

### The AWS profile is per deployment

The auth core's Identity Pool exchange writes a profile into the user's own
`~/.aws/credentials`, under a fixed name. Three deployments would each overwrite
the other two's AWS credentials — the same class of crossing, in a file we do not
own. Named deployments get `adp-deployment-<stable-id>`, shared by all aliases;
the legacy deployment keeps
`bedrock-gateway`, because an existing user has `AWS_PROFILE=bedrock-gateway` in
their shell profile and scripts.

`adp status --json` reports `aws_profile`. Older alias-derived
`bedrock-gateway-<name>` profiles are retired on the next login, refresh or logout;
scripts using those names should switch to the reported stable profile. Removing
an alias deletes its old profile, and final local removal deletes the stable
profile as well. Auth and removal share one file-update lock, preserving unrelated
AWS profiles.

---

## 5. Concurrent agent sessions

`adp codex` runs a loopback proxy that holds the session, so two Codex sessions
on one machine is the case where a fixed port breaks.

- A **named** deployment asks the OS for a free port (`--port 0`) until `codex
  setup` reserves a stable port for bare Codex. Subsequent launches and its
  deployment-specific daemon use that saved port; a collision fails rather than
  routing through another deployment's listener.
- The **legacy** deployment keeps the fixed port it has always used, so a proxy
  started by an older CLI or by `adp daemon install` is still found.

Because a named deployment's port is only known at runtime, the proxy
**publishes** it: `runtime/proxy.json` records `{pid, process_start, proxy, port,
deployment_id, deployment, gateway_url}`. Startup and removal verify the durable
process identity before treating a recorded PID as owned. Discovery is then by
published record, and the record
is validated twice:

1. a record naming a **different** deployment is never reused, even though it
   sits in this deployment's runtime directory — that is the crossed-context bug
   itself;
2. the process actually listening on that port is **asked** which deployment it
   serves, and its answer must match. A bare TCP probe cannot tell this
   deployment's proxy from another's, or from an unrelated local service that
   happens to hold the port.

A proxy that answers with an empty id is a legacy single-deployment proxy, which
is how a running pre-upgrade session keeps working.

Claude setup saves the endpoint and a shell-quoted helper pinned to the stable
ID, URL and store. Each `adp claude` launch supplies its own `--settings` overlay
with that same binding; unrelated supplied settings and arguments survive, and
normal launches do not rewrite global settings. Named `adp codex` launches supply
the selected provider, proxy URL and authentication configuration per process.
Conflicting transport overrides are rejected.

Direct auth and Python helpers resolve through the same module. Before using a
session, they verify that its configured URL matches the selected binding. A
corrupt registry or missing resolver never silently selects the legacy account.
A named alias of the legacy store retains its registered URL; if the legacy store
is rebound, that alias refuses to use its credentials until explicitly repaired.

Running named commands record leases containing their PID and process start time.
Removal checks those leases under the registry lock, so a Claude session or login
protects its store even without a Codex proxy. Dead leases do not prevent removal.
The last named alias deletes the private local store; legacy adoption never does.

> `ADP_PROXY_PORT` pins one port and must **not** be set when running concurrent
> sessions. It is a single-deployment debugging aid.

---

## 6. What deliberately did not change

- **`adp codex` and `adp claude` dispatch.** Frozen by the command contract.
  `--deployment` remains a global option before the verb. Tool arguments are
  forwarded except conflicting overrides of ADP-managed transport settings.
- **A machine with nothing registered.** It resolves with source `legacy` and
  behaves exactly as before, including messages: reporting "Deployment: default"
  there would introduce a concept the user has never met. The deployment name is
  printed only once real deployments exist.
- **Install, update and rollback cover the new file.** `adp_deployments.py` is in
  `CLI_FILES` in both `adp` and `install.sh`, so it is staged, committed,
  `*.prev`-preserved and uninstalled with everything else. Partial downloads
  still never leave a working `adp` beside a missing helper.

### Rolling back past this release

`adp update --rollback` restores the previous copies of the CLI **executables**.
It does not, and should not, touch `~/.adp/deployments.json` or any deployment's
store: a rollback of the software is not a request to log three terminals out.

The consequence is that rolling back to a release from *before* this change
leaves the registry on disk while the restored `adp` has no `adp_deployments.py`
and no `deployment` verb. That older CLI then behaves the way it always did — it
uses `~/.bedrock-gateway` and ignores the registry entirely. Named deployments
are invisible until you `adp update` forward again, at which point they reappear
unchanged. This is a documented limitation rather than a bug: the alternative,
deleting the registry on rollback, would destroy state the user did not ask to
lose.

---

## 7. Two defects this work surfaced, and their fixes

Both were found by driving the real installed CLI rather than its python API, and
both have regression tests.

1. **`adp deployment list --json` only worked in one argument position.**
   `adp deployment --json list` failed in the bash front door while the identical
   `adp deployment list --json` succeeded — a difference with no reason a user
   could infer. The front door now accepts the flag on either side of the
   subcommand.

2. **`adp update` could not find a gateway URL when the default had no session.**
   It read the URL only from the selected deployment's `config.json`, which
   sign-in writes. Since `deployment add` makes no request (§3), a deployment
   that was added but not yet signed in to has no `config.json` — and once such a
   deployment was the saved default, `adp update` failed with "No gateway URL …
   cannot tell where to update from" even though the registry had held its URL
   since the add. It now falls back to `ADP_DEPLOYMENT_URL`. The CLI is installed
   once, not per deployment, and every deployment serves the same CLI, so any
   registered URL can answer the question; refusing to update the software
   because one of several deployments is not signed in helps nobody.

---

## 8. Evaluation

The existing CLI-uplift evaluation framework (`tests/e2e/cli_uplift/`, runbook
[cli-uplift-evaluation.md](../runbooks/cli-uplift-evaluation.md)) is **extended**,
not replaced: two cases, **E16** and **E17**, and one new suite,
`multi-deployment`.

Their fixture is the one thing the workflow cannot create for itself — three
separately reachable ADP deployments, each with its own sign-in fixture — so it
is supplied as a JSON array in the `CLI_UPLIFT_EVAL_DEPLOYMENTS` repository
variable, naming a Secrets Manager secret per deployment and never a password.
Two rules are enforced before a run starts, because breaking either produces a
green result that proves nothing:

- **Distinct gateway URLs**, since three names over fewer URLs are aliases (§3)
  sharing the very session whose independence is under test;
- **a credential secret reference per deployment**, so each gateway's login is
  independently configured. The current fixture format requires distinct secret
  names. User IDs may coincide across independent deployments.

With the variable unset, E16/E17 report `blocked` naming `three_deployments` and
`full_acceptance` stays false. Even with reachable gateways, model execution is
disabled by the separate `multi_deployment_model_limits` requirement and a remote
entry-point guard. Hard Codex output limits (at most 256 tokens per request) and
the aggregate 48-request ceiling must be implemented before inference. There is
no configuration override. The zero-model-request session checkpoint remains
available; supplying gateway fixtures alone cannot enable live acceptance.

The offline suites (`modules/gateway/tests/cli/`, `tests/unit/test_cli_uplift.py`)
cover the registry, the precedence rule, concurrency, the proxy identity checks,
install/update/rollback with three records, and the fixture rules above. They do
not and cannot establish AC-10/AC-11, which require real CLI, Claude and Codex
execution against three live gateways on disposable EC2. Those remain open.

The live runner correlates tool requests with `X-Request-ID` in usage records;
prompt text is not a field in the usage API. It requires three completed,
overlapping processes, all three proxy identities after both arrangements, and
successful cleanup. E17 holds three real tool processes at local shell barriers,
changes the default, refreshes one session, logs another out, then requires the
same processes to continue or fail with an authentication error as appropriate.
These paths still need execution against the three real deployment fixtures on
EC2. Offline tests validate the harness decisions, not live acceptance.
