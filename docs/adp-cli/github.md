# GitHub setup for administrators and users

[CLI guide](README.md) · [Command reference](command-reference.md)

An ADP administrator configures the deployment's GitHub App. Individual users
then connect repositories through that App. The commands use the existing ADP
deployment and the same ADP login as the rest of the CLI.

## Configure the deployment's GitHub App

GitHub setup happens after ADP platform deployment. A native Cognito
administrator can sign in without first enabling GitHub sign-in:

```bash
adp admin login
adp admin github status
```

`adp admin setup --org example-org` also offers this GitHub step alongside Bedrock
setup. Being an ADP administrator does not make you a GitHub organization owner.

### Create a new App

```bash
adp admin github setup --new --github-org example-org
# Complete GitHub's App-creation approval, then resume:
adp admin github setup
adp admin github status
```

The CLI prepares GitHub's manifest flow and opens its approval page. It prints
the handoff location for a headless session. GitHub's owner approval creates
the App; until that happens the command reports pending with exit code 4.
Rerunning resumes setup rather than creating a second registered App.

Use `--app-name NAME` for an explicit App name and `--visibility public` if
other organizations must be able to install it. Visibility defaults to
`private`. Use `--owner user` to create an App owned by your personal GitHub
account instead of an organization.

`--github-org` selects the GitHub organization that owns the App. `--org`
selects an ADP organization context. They are separate values; do not substitute
one for the other.

### Import an App another GitHub administrator created

The App owner can provide the existing App credentials through a private file:

```bash
adp admin github setup --existing --credentials-file /private/github-app.json
adp admin github revalidate
```

The JSON file must be owned by you with permissions `0600`. Example structure
with placeholder values:

```json
{
  "app_id": "123456",
  "private_key_file": "/private/github-app.pem",
  "client_id": "CLIENT_ID_FROM_GITHUB",
  "client_secret": "CLIENT_SECRET_FROM_GITHUB",
  "webhook_secret": "WEBHOOK_SECRET_FROM_THE_APP_OWNER"
}
```

The PEM file must also be private (`0600`). `private_key` accepts the PEM inline
instead of `private_key_file` when feeding JSON from a secret manager.
`--credentials-stdin` accepts the same object through a pipe without putting
secrets in command arguments.

Importing without OAuth `client_id` and `client_secret` leaves GitHub sign-in
unconfigured and reports the missing values. It does not prove login works.
If another App is already registered, the CLI refuses to silently replace it;
an administrator must deliberately disconnect it in Settings → Connections.

### Check configuration

```bash
adp admin github status --json
adp admin github revalidate --json
```

Status separates GitHub sign-in, repository installations and agent integration.
Revalidate reads GitHub's live configuration and reports corrections needed
from the App owner; it does not edit GitHub to make the check pass.

Stored OAuth credentials do not prove a successful browser login. Compare the
expected callback/webhook URLs with the App configuration, and complete a real
GitHub sign-in to verify the login flow. Repository configuration similarly
does not prove an agent has completed a task.

## Connect your repository

Once the deployment has a GitHub App:

```bash
adp login
adp github connect --repo example-org/project
adp github status --repo example-org/project
```

On EC2 or another headless machine:

```bash
adp github connect --repo example-org/project --no-browser
```

Open the printed URL and approve the repository on GitHub. `--repo` names the
repository you want; selecting it and granting access happen on GitHub.
The CLI verifies access to that exact repository afterwards. A user does not
need to supply a PAT, App private key or OAuth client secret for this flow.

If your GitHub organization requires owner approval, the command reports
pending. After approval, rerun the same command:

```bash
adp github connect --repo example-org/project
```

An existing suitable installation is reused. `--org ADP_ORG` can narrow results
to an ADP organization you belong to; it does not grant additional access.

Status distinguishes verified repository access, access that could not be
checked, and access GitHub has not granted. `agent_integration` describes
configuration; these commands do not submit an issue, launch an agent or prove
a completed agent run. If no GitHub App is configured, an ADP administrator
must finish the setup above first.
