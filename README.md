# ADP — Agentic Developer Platform

## Use any agentic coding tool of your choice. Scale to hundreds of developers.

Connect Claude Code, Codex, or Kimi Code through one ADP gateway. Developers sign in with GitHub using short-lived, automatically refreshed credentials. As teams grow, organizations can enforce budgets and rate limits at every level.

- **Choose your coding tool with one simple setup.** Install the ADP CLI once and connect Claude Code, Codex, or Kimi Code to the same ADP gateway. Each tool has a one-time configuration step, then you can use the tool that fits your work through one ADP connection.
- **Sign in with GitHub, without long-lived API keys.** Use GitHub sign-in instead of managing a separate model API key. ADP gives your machine short-lived credentials and refreshes them automatically, without requiring individual AWS credentials for everyday tool use.
- **Enforce spending controls across your organization.** Set and enforce budgets and rate limits for hundreds of developers at the organization, department, team, and user levels, with usage and cost visibility at each level.

## Run coding agents in the cloud for complex long horizon tasks

- **Delegate from GitHub to the cloud.** Mention a [cloud-hosted agent](modules/agent-factory/) in an issue or pull request. ADP runs the task without requiring you to keep a local coding session open.
- **Use the right agent persona.** Product and PM agents shape and coordinate work; architect, developer, reviewer, and operations agents handle design, implementation, review, and deployment.
- **Choose a Claude or Codex agent stack.** ADP supports two cloud agent stacks: one built on the Claude Agent SDK and one on the native Codex SDK. Mention `@agent-developer` or `@agent-codex-developer` to choose a stack for implementation.

## Getting started

### Connect Codex to an existing ADP deployment

You need Codex installed, an approved ADP account, and a deployment with GitHub sign-in enabled. Replace `https://YOUR_ADP_DOMAIN/api` with your deployment's URL.

1. **Install the ADP CLI** from your deployment:

   ```bash
   curl -fsSL https://YOUR_ADP_DOMAIN/api/cli/install.sh -o adp-install.sh
   sh adp-install.sh --gateway-url https://YOUR_ADP_DOMAIN/api
   ```

   The installer adds `~/.adp/bin` to your shell configuration. Open a new terminal before the next step.

2. **Sign in with GitHub:**

   ```bash
   adp login
   ```

   Confirm that the short code in your terminal matches the browser approval page, then sign in with GitHub and approve the connection.

3. **Configure Codex:**

   ```bash
   adp codex setup
   ```

4. **Run Codex through ADP:**

   ```bash
   adp codex
   ```

The same ADP login works across your coding tools. For Claude Code, run `adp claude setup` and `adp claude` after signing in. Kimi Code uses the same login but needs the separately configured ADP Kimi adapter. See **CLI Setup** in your deployment for tool-specific instructions. If your sign-in session expires, run `adp login` again.

### Deploy ADP in your AWS account

From a clean checkout, choose an AWS profile and a published release. Confirm the target account **before** installing:

```bash
aws sts get-caller-identity --profile YOUR_PROFILE \
  --query '{Account:Account,Arn:Arn}' --output table
./deploy.sh --aws-profile YOUR_PROFILE --release YOUR_RELEASE
```

Replace the placeholders with your profile and an existing release tag. Follow the [deployment quickstart](docs/adp-platform-deployment/deploy-quickstart.md) for prerequisites, installation, upgrades, and verification. An AI coding agent can follow the [agent deployment guide](docs/adp-platform-deployment/deploy-with-agent.md). After deployment, enable GitHub sign-in and approve developer accounts before using the Codex steps above.

For a deeper view of the platform, see [ARCHITECTURE.md](ARCHITECTURE.md) and the [hosted agent catalogue](docs/agent-catalogue.md).

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow. ADP is licensed under the [Apache License 2.0](LICENSE).
