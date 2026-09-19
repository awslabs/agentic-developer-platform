# Connecting a GitHub repository with the ADP CLI

Connect a repository you already have access to on GitHub, so ADP agents can
work in it:

```sh
adp github connect --repo example-org/project
adp github status --repo example-org/project
```

You supply no GitHub secrets. These commands use the GitHub App your ADP
deployment already has — you never create an app, and you are never asked for a
private key, client secret or webhook secret. If you are asked for any of those,
something is wrong; stop and tell your ADP administrator.

## What actually happens

`connect` asks ADP for an installation URL and opens it in your browser. The URL
is always printed first, so an SSH session or a machine with no browser can
finish the same flow by hand — or pass `--no-browser` to skip the attempt
entirely.

**You choose the repository on GitHub, not here.** GitHub's installation screen
owns that choice, and ADP cannot make it for you. So `--repo` is the repository
you are *asking* for; it is not proof you selected it, and it grants nothing on
its own. After the installation exists, the CLI checks GitHub for access to that
exact repository. A different repository in the same installation does not
satisfy the request — if you asked for `example-org/project` and selected
`example-org/other`, you are not connected to `project`.

Naming an organization or repository you do not have access to cannot give you
access to it. Authorization is decided by the server from your ADP membership
and your GitHub permissions, and the request carries no tenant argument at all.
`--org` only narrows the results to one ADP organization you already belong to;
it cannot widen them, and it cannot move an installation between organizations.

## When GitHub needs someone else to approve

If the owner is an organization you do not administer, GitHub sends your request
to its owners and the installation waits. That is not a failure — the command
reports `pending` with exit code 4, names who must act, and remembers what you
asked for:

```sh
adp github connect --repo example-org/project    # pending: awaiting org approval
# ... an owner of example-org approves ...
adp github connect --repo example-org/project    # resumes; no second installation
```

Rerunning is always safe. If an installation already covers the repository, it is
reused and reported as such — you do not accumulate duplicates.

If the owner is already connected to ADP but this repository is not among its
authorized repositories, that is a repository-selection problem rather than a
missing installation, and the CLI points you at GitHub's page for the
installation you already authorized.

## Reading `status` honestly

`status` answers three separate questions, and deliberately does not blur them:

| Question | What proves it |
|---|---|
| Are you signed in to ADP? | A successful authenticated call — not the presence of a local token file |
| Does ADP have access to this repository? | GitHub listing it in a live read, just now |
| Is the agent integration configured? | Tenant credentials and webhook routing being in place |

The third one is the one to read carefully. It reports **configuration only**.
Nothing in these commands runs an agent, so a `configured` result never means an
agent has successfully completed work in your repository — the output says so
explicitly, and `agent_run_observed` is always `false`.

Repository access is reported four ways, because "we could not check" is not the
same as "you do not have it":

- `verified` — GitHub confirmed this exact repository, just now.
- `unproven` — ADP has it on record but could not reach GitHub to confirm. This
  is configuration, not proof. Retry shortly.
- `not_granted` — GitHub was reached and does not list it.
- `unknown` — could not be determined either way.

A check ADP could not perform is never reported as a failure.

## Scripting

Add `--json` to any command for a single machine-readable object on stdout, with
stable identifiers (installation IDs, repository full names, tenant IDs) and no
secrets. Exit codes:

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Usage error |
| 2 | Sign-in required — run `adp login` |
| 3 | Not authorized |
| 4 | Waiting on an action in GitHub; rerun to resume (**not** a failure) |
| 5 | The operation failed |

Treat 4 as "come back later", not as an error. Use `--dry-run` to see what a
command would do without changing anything.

Connection state is shared with the ADP web UI — a repository connected here
appears under Settings → Connections, and one connected there is visible to
`adp github status`.

## If your deployment has no GitHub App yet

```
$ adp github connect --repo example-org/project
This ADP deployment has no GitHub App yet. An ADP platform administrator must
set one up (Settings > Connections, or adp admin github). Ordinary users cannot
and should not create it — it needs the platform's own app credentials.
```

Status `unavailable`. This is a dependency on your platform, not something to
work around: creating a second app with your own credentials would not connect
you to ADP. Send the message to whoever administers your deployment.

## Validation status

The behaviour above is covered by automated tests against a mocked gateway,
including that no repository is ever sent to the install-start API, that the
saved local state holds no credentials, and that installation metadata alone is
never reported as a passing agent run.

Live end-to-end acceptance for an ordinary user against a configured GitHub App
runs on the shared EC2 test infrastructure, not on an operator's machine. Mocked
coverage is not a substitute for that gate.
