# Finding out what works, and why something failed

Two commands answer questions the CLI used to leave you guessing at:

```sh
adp capabilities                    # what this deployment offers YOU
adp capabilities --operation flows.approve.write   # just one operation
adp doctor                          # why did that fail?
adp doctor --checks auth,budget     # only the checks you want
adp doctor --request-id REQUEST_ID      # explain one of your past requests
```

Both only **read**. Neither writes configuration, starts work, nor runs a paid
model call — so you can run either one against a deployment that is already
misbehaving without wondering what you just touched. Neither fixes anything
either: they tell you the cause and, where one exists, the command that would fix
it.

## `adp capabilities` — whose problem is it?

When something is unavailable there are four quite different reasons, and the
useful answer is *which one*:

| It says | What it means | What to do |
|---------|---------------|------------|
| `not available here` | This deployment has never heard of the operation | `adp update`; if that does not help, the gateway needs upgrading |
| `switched off on this deployment` | The module exists but is disabled | Ask your ADP administrator to enable it |
| `not permitted for you` | It is on, but you may not do it | Ask your ADP administrator for access |
| `a service it needs is not ready` | You are allowed; a dependency is not usable | Usually temporary — try again shortly |
| `available, with something unconfirmed` | Nothing blocks it, but one fact could not be established | Try it; the server decides |
| `available` | Everything checked out | Go ahead |

Those are four **independent** facts — supported, enabled, permitted, ready —
and the command keeps them apart on purpose. Collapsing any two gives confident
nonsense: a disabled module reported as a permission problem sends a fully
authorized administrator to ask for access they already have.

"Unconfirmed" is a real answer, not a hedge. Some things genuinely cannot be
settled by reading configuration — whether an agent worker is *running right now*
is one, because pods scale from zero and the only proof is to start work, which
these commands will not do. The command says so rather than guessing.

### It never grants anything

Capability discovery narrows what the CLI bothers to send; it authorizes nothing.
The server still checks every request. Where the CLI has **definitive** evidence a
mutation cannot work it refuses before sending, so a doomed write is not
attempted — but where evidence is missing, stale, or the gateway is unreachable,
it proceeds and lets the server decide. It never falls back to an older code path
to get a write through.

Answers are cached briefly per deployment, identity and tenant. `--refresh`
re-reads. A cached answer is never reused across deployments or identities, so
switching either gets you a fresh one.

## `adp doctor` — diagnosing a failure

Five checks, all reads:

| Check | Answers | Notes |
|-------|---------|-------|
| `auth` | Are you signed in, and until when? | Expiry only — never prints your token |
| `api` | Is the gateway reachable, and what does it speak? | Reports its release, or `unknown` if it does not publish one |
| `budget` | Is a spending limit blocking you? | A `soft` limit warns and allows; only `hard` blocks |
| `models` | Is there a model route? | Resolved from configuration — **never** by sending a prompt |
| `agents` | Are hosted workers available? | Says plainly when "running right now" cannot be read |

A failing check never aborts the others. The first failure is often a symptom of
a later one, so you get the whole picture.

Output is redacted by field name before printing, because these reports get
pasted into tickets and chat.

### `--request-id`

Explains gateway request metadata only when the signed-in caller holds log-read
permission in that tenant. A foreign-tenant, unauthorized, or absent request ID
produces exactly the same answer — identical wording, code and exit status. The
response omits user IDs, query parameters, headers, bodies, client addresses and
provider details.

## Output and exit codes

The existing envelope and exit codes, unchanged. `--json` gives one JSON object
per command (neither command watches anything, so there is no NDJSON stream):

```json
{"status": "ok", "command": "doctor", "detail": {"checks": {}}, "next_action": null}
```

| Exit | Meaning |
|------|---------|
| 0 | Everything checked out |
| 1 | Usage error — nothing was sent |
| 2 | Not signed in |
| 3 | Not permitted |
| 4 | Pending, unavailable, or undetermined |
| 5 | Failed |

Errors carry a stable `code` a script can branch on: `unsupported_operation`,
`feature_disabled`, `permission_denied`, `stale_revision`, `budget_exhausted`,
`dependency_pending`, `request_timeout`, `unknown_mutation_outcome`,
`capability_unknown`, `schema_unsupported`. These use the exit categories above
without changing established authentication or tool-launch exits — anything
already branching on 2 or 3 keeps working.

`unknown_mutation_outcome` is the one to handle carefully: it means a change may
or may not have been applied. Read the current state before retrying rather than
repeating the command.

## What ships today

Everything on this page is **shipped** — both verbs, all five checks, all flags
above. `modules/gateway/cli/command-manifest.json` is the machine-readable
version, and it marks each command `shipped` or `proposed`. Tests hold it against
the real dispatcher, the install and update lists, the download allowlist and the
server's capability contract in both directions, so a command cannot be
documented here without shipping, and cannot ship without appearing there.

The server side is `GET /me/cli-capabilities` — authenticated and scoped to your
own tenant, with no target parameter at any position. Pre-login information comes
from the existing public installation discovery instead; no tenant data is exposed
publicly.

**Not** shipped, and out of scope by design: automatic remediation, any paid probe
to test a model, granting yourself a permission, and a plugin framework. The
manifest is a checked list, not a loader — adding an entry cannot create a
command.

If advisory capability discovery is unavailable or has unknown axes, mutation
commands warn on stderr and retain the observation in the single JSON result's
`capability_preflight` field, keyed by operation ID. The existing server route
still performs authorization; this metadata grants no permission.
