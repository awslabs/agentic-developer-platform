# GitHub App configuration with the ADP CLI

An ADP platform administrator wires GitHub sign-in and repository access for the
whole deployment:

```sh
adp admin github setup --new --github-org example-org
adp admin github status
adp admin github revalidate
```

GitHub is connected **after** the platform is deployed. These commands call the
deployed ADP APIs — they provision no AWS infrastructure and run no Terraform.
`adp admin setup` offers the same GitHub step as part of guided setup and can
resume it on a later run.

`--github-org` is the **GitHub organization that will own the App**. `--org` is
the **ADP organization** context. They are different things and the CLI never
substitutes one for the other; add `--owner user` to deliberately create an App
under your personal GitHub account instead.

## Create a new GitHub App

```sh
adp admin github setup --new --github-org example-org
# Approve the App's ownership and permissions on GitHub, then:
adp admin github setup
```

ADP generates the App manifest — webhook URL, callback URL, permissions and event
subscriptions — and the CLI stages GitHub's required form submission as a
single-use private page under `~/.adp/state/`, opens it, and prints the path so an
SSH session or a machine with no browser can still finish by hand. The App does
not exist until a GitHub owner of that organization approves it there, so the
command exits **4** (pending an external human action, not a failure) and the
next run resumes from the App's registered state. If an App is already registered
for the deployment, setup reports its status instead of creating a second one.

Add `--app-name` to choose the name (GitHub requires it to be unique across all
of GitHub) and `--visibility public` if organizations other than the owner must be
able to install it. The default is private.

## Connect an App another GitHub administrator created

When the GitHub App owner is not you — a common case, since ADP administrative
privileges do not imply GitHub organization-owner privileges — they create the App
and hand you its credentials:

```sh
adp admin github setup --existing --credentials-file ./github-app.json
```

The file must be a regular file you own with permissions `0600`:

```json
{
  "app_id": "987654",
  "private_key_file": "/private/path/app.private-key.pem",
  "client_id": "Iv1.example",
  "client_secret": "…",
  "webhook_secret": "…"
}
```

The private key is read from the `0600` `.pem` file the App owner sent you;
`private_key` accepts the PEM inline for a secret manager. Use
`--credentials-stdin` to pipe the same JSON object from one. No credential value
is ever a command argument, and nothing is written to local state.

Omitting `client_id`/`client_secret` imports the App with **GitHub sign-in off** —
the command reports `pending` and names the missing values rather than implying
setup finished. Rerun with the same App ID once the owner provides them.

If the deployment already uses a *different* App, importing is refused: repointing
a shared App would break its existing consumers. Disconnect the current App
deliberately in Settings → Connections first. ADP never rotates a key or repoints
a webhook on its own to make a check pass.

## What status actually proves

```sh
adp admin github status --json
```

Three areas are reported **separately**, because they fail independently:

| Area | Reports |
|---|---|
| `sign_in` | Whether OAuth credentials are stored, plus the callback URL to compare |
| `repositories` | Whether the App can be installed, and how many installations have complete records |
| `agent_integration` | Webhook secret, webhook URL, permissions and event subscriptions |

**Configured is not verified.** GitHub exposes no API that reads back an App's
configured OAuth callback URL, so stored credentials can only ever be reported as
`configured` — status prints the expected callback URL and a deep-link to the
App's settings page so you can compare by eye. A real "Sign in with GitHub" round
trip is the only thing that proves login works. Checks that could not be
determined read `unknown`, never `ok`, and any incomplete area keeps the overall
result `pending`: missing credentials, a mismatched webhook or an incomplete
tenant installation record cannot produce a ready claim.

`revalidate` re-reads the App's live configuration on GitHub. When it finds drift
— someone edited the App's settings, and GitHub fires no event when they do — it
names what the GitHub App owner must correct. It changes nothing on GitHub.

## Automation

`--json` prints one object on stdout following the
[shared CLI contract](../../../docs/design-notes/5180-cli-command-contract.md):
`status`, `command`, `detail`, `next_action`, and `error` on failure. Secrets never
appear in it. `--dry-run` reports what would change and exits 0 without
configuring anything. `--yes` skips the ownership confirmation; it never skips a
credential prompt.

Exit codes: **0** configured or a completed read, **1** usage error, **2** sign in
again (`adp admin login`), **3** authenticated but not a platform administrator,
**4** waiting on the GitHub App owner or on an OAuth login that has not happened
yet, **5** the operation failed. Scripts distinguish "waiting on a human" from
"broken" by 4 versus 5.
