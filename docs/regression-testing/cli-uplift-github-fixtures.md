# CLI-uplift evaluation — GitHub fixtures (owner action required)

The GitHub cases of the broader CLI-uplift evaluation (#5256) — **E10, E11,
E12** — need GitHub fixtures that only a GitHub organisation owner can create.
This document states exactly what is needed, why it cannot be self-served, and
how to verify it once done.

## Why this is an owner action

Creating a GitHub App, approving an OAuth authorisation and installing an App on
a repository are all account-owner actions by GitHub's design. They cannot be
performed by an integration token, and the evaluation identity is an
integration:

```
$ gh api user
403 Resource not accessible by integration
$ gh api user/orgs
403 Resource not accessible by integration
```

There is no API path around this and there should not be — the whole point of
the OAuth/installation approval flow is that a human owner consents. A seeded
token would not be a login, and E11 specifically asserts a *real* OAuth approval,
so faking it would invalidate the case it is meant to prove.

**Out of scope, deliberately:** the shared production GitHub App is not reset,
reconfigured or borrowed. Resetting it would break real users. The fixtures below
are new and isolated, which is also why `config.validate()` has no fallback to a
shared App.

## What is needed

Three values, supplied as repository variables. `bindings.dev.json` leaves all of
them absent on purpose, so the GitHub cases currently **block** rather than pass
on something weaker.

| Variable | Value | Used by |
|---|---|---|
| `CLI_UPLIFT_EVAL_GITHUB_ORG` | The organisation that owns the fixtures | E10, E11, E12 |
| `CLI_UPLIFT_EVAL_GITHUB_APP` | Name of an **existing** App fixture, for the reuse half of E10 | E10 |
| `CLI_UPLIFT_EVAL_GITHUB_REPO` | A **dedicated** evaluation repository | E11, E12 |

### 1. Organisation

Any organisation the owner controls and is willing to have the evaluation act
within. It must not be an organisation with real production repositories that
matter, because E12 creates a branch and a pull request.

### 2. An existing App fixture (`github.existing_app_fixture`)

E10 has two halves: the native-admin journey **creates a fresh App**, and it also
**reuses an existing App**. The fresh one the evaluation makes itself — that is
the flow under test. The pre-existing one has to be created by the owner, because
"reuse an App that already existed" cannot be tested with an App the run just
made.

Create it via **Settings → Developer settings → GitHub Apps → New GitHub App** in
the fixture organisation:

- Name: anything stable, e.g. `adp-cli-uplift-eval-existing`
- Homepage URL: the dev gateway URL
- Webhook: may be inactive for this fixture
- **Visibility: private** (owner-only). The evaluation's own
  `register-github-app.sh` is private-by-default for the same reason.
- Permissions: repository `Contents: read & write`, `Pull requests: read &
  write`, `Metadata: read`. These are what the agent-development task in E12
  needs; nothing organisation-wide and no admin scope.

Install it on the dedicated repository below, and record its name (not its
private key — the evaluation never receives one; see "No secrets in this
request").

### 3. A dedicated repository (`github.repo`)

E12 completes "one bounded existing agent-development task ... with webhook/run
and commit/PR evidence". It must be a **dedicated** repository, not a shared one:

- The run pushes a branch and opens a pull request.
- E11 asserts that the *wrong* repository, a cross-tenant attempt and a nonce
  replay all **fail** — those negative assertions need a repository whose
  expected state is known.

Suggested: a new, private, near-empty repository, e.g.
`adp-cli-uplift-eval-fixture`, with a README and a default branch. Branch
protection is not required and, if enabled, must not be bypassed — the evaluation
does not weaken protections.

### 4. The OAuth approval during the run (E11)

E11 exercises `adp login` with a real OAuth/browser approval. When the run
reaches it, an owner must complete the authorisation prompt. Notes:

- The browser step runs **on the disposable EC2 instance**, not on a laptop, and
  it respects the service's access controls — it does not bypass a consent
  screen.
- This is the one point in the evaluation where a human action is required
  mid-run and cannot be pre-staged, because consent is the thing being tested.

## No secrets in this request

The evaluation asks for **names and identifiers only**: an org login, an App
name, a repository name. No private key, client secret, token, password or
webhook secret is requested here or should be pasted into any config file.
`config.no_secrets()` structurally refuses a credential value in the bindings
file, so a pasted secret fails the offline guards rather than reaching a commit.
Where the run needs App credentials it reads them at run time from Secrets
Manager through its own scoped grant.

## Applying it

Set the three repository variables (they layer over `bindings.dev.json`, so a
variable always wins and no file change is needed):

```bash
gh variable set CLI_UPLIFT_EVAL_GITHUB_ORG   --body '<org-login>'
gh variable set CLI_UPLIFT_EVAL_GITHUB_APP   --body 'adp-cli-uplift-eval-existing'
gh variable set CLI_UPLIFT_EVAL_GITHUB_REPO  --body '<org-login>/adp-cli-uplift-eval-fixture'
```

## Verifying it worked

Dispatch `eval-cli-uplift.yml` with `suites=github`. Preflight proves each
fixture class independently, so a partial setup names precisely what is missing
rather than failing opaquely:

```
Unavailable fixture classes:
  github_app: an isolated GitHub App fixture (config github.org + github.app_fixture/existing_app_fixture)
  github_repo: a dedicated evaluation repository (config github.repo)
```

When both are satisfied, E10/E11/E12 move from `blocked` to executing.

## What remains blocked without this

E10, E11 and E12 stay `blocked`. They are graded `blocked`, never `passed` — the
grader cannot be talked out of it — so `full_acceptance=true` is unreachable
until these fixtures exist. Every non-GitHub suite is unaffected and continues to
run, which is why the evaluation proceeds with `install`, `admin`, `parity` and
(once the destination roles land, see
[`cli-uplift-destination-roles.md`](cli-uplift-destination-roles.md)) the
destination suites in the meantime.
