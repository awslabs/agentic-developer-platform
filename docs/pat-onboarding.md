# GitHub PAT Onboarding Guide

Register a GitHub Personal Access Token (PAT) so ADP agents can create pull
requests and post comments **as you** instead of as a bot account.

---

## Why a PAT?

ADP supports two execution modes for GitHub operations:

| Mode | Attribution | Setup required |
|------|-------------|----------------|
| **GitHub App** (default) | Bot account (`adp-agent[bot]`) | Org owner installs App |
| **Personal Access Token** | Your GitHub identity | You paste a token |

PAT mode is ideal for:
- Personal repos (no org admin needed)
- Teams that want PRs attributed to the requesting human
- Environments where GitHub App installation is blocked

---

## Step 1: Create a Fine-Grained PAT

1. Go to [GitHub > Settings > Personal Access Tokens > Fine-grained](https://github.com/settings/personal-access-tokens/new)
2. Set **Token name**: e.g. `adp-agent-pat`
3. Set **Expiration**: choose an appropriate duration (30, 60, or 90 days recommended)
4. Set **Repository access**: select the repos you want the agent to operate on
5. Under **Permissions**, grant:

| Permission | Access | Why |
|------------|--------|-----|
| Contents | Read & Write | Clone, push, branch creation |
| Issues | Read & Write | Read issue body, post comments, update labels |
| Pull requests | Read & Write | Create PR, push to PR branch, request reviewers |
| Metadata | Read | Required for all fine-grained PATs |

Optional (persona-dependent):
- **Checks** (Read & Write): If the agent posts check-run annotations
- **Actions** (Read): If the agent monitors workflow runs

6. Click **Generate token** and copy it immediately (it won't be shown again).

---

## Step 2: Register in ADP

1. Navigate to **Settings > My Credentials**
2. In the **GitHub Personal Access Token** section, click **+ Register GitHub PAT**
3. Paste your token into the password field
4. Optionally set the **Expiry Date** to match the token's GitHub expiry
5. Click **Register PAT**

The token is stored securely in AWS Secrets Manager. It is never returned in API
responses or displayed in the UI after registration.

---

## Expiry and Rotation

GitHub fine-grained PATs have a maximum lifetime (configurable by your org, up to
1 year). When your token nears expiry:

- The credentials page shows the registered expiry date so you can track it
- To rotate: delete the old credential, create a new PAT in GitHub, register it

**Rotation steps:**
1. Create the new PAT in GitHub (Step 1 above)
2. In ADP, remove the old credential (click **Remove** on the PAT card)
3. Register the new token (Step 2 above)

Agent runs that start before rotation complete normally (GitHub PATs remain valid
until their actual GitHub-side expiry, not the ADP-registered date).

---

## Self-Review Limitation

PRs created with your PAT are authored by **your GitHub account**. This means:

- You **cannot approve your own PRs** if branch protection requires reviews
- A teammate must review and approve agent-generated PRs
- For solo personal repos without branch protection, this is not a concern

If your repo requires CODEOWNERS approval and you are the sole CODEOWNER, PAT
mode may not be suitable. Consider using the GitHub App mode instead.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Agent fails with "token expired or revoked" | PAT expired or deleted in GitHub | Create a new PAT and re-register |
| Agent fails with "Resource not accessible" | PAT lacks required permissions | Regenerate with correct permissions (see Step 1) |
| Agent fails with "Not Found" on a private repo | PAT not scoped to that repo | Edit PAT in GitHub to include the repo |
| Cannot register: "already exists" | A PAT is already registered | Remove the existing one first, then register |

---

## Security Notes

- PAT values are stored in AWS Secrets Manager with `strict=true` (only you can access them)
- The PAT is never logged, displayed after registration, or included in API responses
- Deleting the credential in ADP removes it from both the database and Secrets Manager
- For maximum security, scope your PAT to only the specific repositories the agent needs
