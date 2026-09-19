---
name: superplane
description: >-
  Use when working with Superplane multi-cloud GPU orchestration — deploying AI
  models on GPUs (vLLM, SGLang), managing GPU workspaces, provisioning or scaling
  nodes across clouds (AWS, Nebius, Lambda Labs), checking GPU costs or getting
  price estimates, submitting training and batch jobs, or getting kubeconfig
  access to a workspace cluster. Read-only discovery (listing, describing,
  estimating) is safe to run freely; provisioning, scaling, deploying and
  deleting spend money or mutate state, so confirm those with the user first.
allowed-tools: Bash Read Write
metadata:
  domain: superplane
  ported_from: src/superplane-skill (EPIC #4910 U4)
  authentication: ADP identity — no skill-managed credential
---

# superplane

Superplane is a multi-cloud GPU orchestration platform: one CLI to deploy AI
models, provision GPU nodes across AWS / Nebius / Lambda Labs, manage workspaces,
and monitor cost.

> **Ported asset (EPIC #4910, unit U4, R9).** ADP port of the upstream
> `src/superplane-skill/` (see
> `modules/domain-apps/ai-super-plane/reference/src/superplane-skill/`). The
> operational content is ported closely; **the authentication model is
> deliberately different** — see [Identity and credentials](#identity-and-credentials).
> Full port notes in [`PORT-NOTES.md`](PORT-NOTES.md).

## Identity and credentials

**This is the part that differs most from upstream. Read it before running
anything.**

You run on the ADP agent runtime under ADP identity. That means:

- **There is no `superplane login` step for you to run.** Upstream this skill
  told the agent to run an interactive login that wrote a JWT to
  `~/.superplane/config.yaml`. On ADP, an agent does not hold a long-lived
  provider token, and an interactive login cannot complete in a worker pod
  anyway.
- **Do not read, write, print or echo `~/.superplane/config.yaml`.** It may hold
  a token. A token printed into your output is copied into the run transcript and
  the log sink; rotation is then the only remedy.
- **Do not read or echo `SUPERPLANE_API_KEY`, `SUPERPLANE_TOKEN`, or any other
  credential environment variable.** Not to check whether it is set, not to
  confirm its prefix, not in a debug line. If you need to know whether you are
  authenticated, run a harmless authenticated command (below) and look at whether
  it succeeded.
- **Never ask the user to paste a token or API key into an issue or comment.** An
  issue comment is a permanent, world-readable disclosure for a public repo. If a
  credential genuinely needs connecting, point the user at
  `/settings/credentials` and stop.
- **If a command fails with an authentication error, stop and report it.** Do not
  attempt to obtain, construct, refresh or work around a credential. An
  auth failure here is a platform wiring issue for a human, not a puzzle for you.

## Before you start

Confirm the CLI is present and working. Two commands, and neither prints a secret.

**Step 1 — is the CLI installed?**

```bash
superplane --version
```

| Output | Meaning | Next action |
|---|---|---|
| a version string | CLI installed | go to step 2 |
| `command not found: superplane` | not installed | stop; report that the worker image lacks the Superplane CLI. Do **not** `pip install` it — see below. |

> **Do not install the CLI yourself.** Upstream told the agent to advise
> `pip install superplane-cli`. On ADP the worker image is built from a
> Dockerfile; a package installed at run time is invisible to the next run,
> unpinned, and unreviewed. A missing CLI is an image gap to report, not to patch.

**Step 2 — does an authenticated call work?**

```bash
superplane workspace list --json
```

| Output | Meaning | Next action |
|---|---|---|
| a list of workspaces (possibly empty) | working | **bootstrap done** |
| `Not authenticated` / 401 | identity not wired through | **stop and report.** Do not try to log in or find a credential. |
| `Could not connect` | API unreachable | stop and report; likely a network/endpoint issue |

**Step 3 — workspace context**, if you are about to run a workspace-scoped
command. If no workspace is set, **ask the user which one** rather than picking:

```bash
superplane workspace use <workspace-name>
```

## Read-only vs. spending

Sort every command into one of these before you run it. This split is the same
one the Superplane MCP tool surface enforces
(`../../../tools/superplane-mcp/`), where read-only discovery is a distinct tool
from anything that spends or mutates.

| Safe to run freely (read-only) | Spends money or mutates state |
|---|---|
| `workspace list`, `workspace describe` | `workspace create`, `workspace delete` |
| `deploy list`, `deploy describe`, `deploy logs` | `deploy`, `deploy delete` |
| `node list`, `node describe` | `node drain` |
| `nodepool list` | `nodepool create`, `nodepool scale`, `nodepool delete` |
| `job list`, `job logs` | `job submit`, `job cancel` |
| `cost`, `cost estimate` | — |

**Before any command in the right-hand column:** state what it will create or
change, the hourly cost if it provisions capacity, and how it gets torn down —
then get the user's confirmation. `cost estimate` is free; use it to produce the
number you are asking them to approve.

Use `--json` on any command whose output you intend to parse. Table output is for
humans and is fragile to parse.

## Essential commands

**Workspaces** — the primary user-facing resource:

| Command | Description |
|---|---|
| `superplane workspace create --name NAME --isolation dedicated` | Create a workspace (dedicated cluster; 10–15 min) |
| `superplane workspace create --name NAME --isolation namespace --cluster C` | Create a namespace workspace on a shared cluster (seconds) |
| `superplane workspace list` | List all workspaces |
| `superplane workspace describe NAME` | Workspace details |
| `superplane workspace use NAME` | Set the current workspace context |
| `superplane workspace kubeconfig NAME` | kubectl access to the workspace cluster |
| `superplane workspace delete NAME` | Delete a workspace (**destructive**) |

**Model deployments:**

| Command | Description |
|---|---|
| `superplane deploy --model MODEL --precision fp8` | Deploy a model |
| `superplane deploy list` | List deployments |
| `superplane deploy describe --name NAME` | Details + endpoint |
| `superplane deploy test --name NAME` | Validate the endpoint |
| `superplane deploy logs --name NAME` | Stream logs |
| `superplane deploy delete --name NAME` | Remove a deployment |

**Nodes and node pools:**

| Command | Description |
|---|---|
| `superplane node list` | Nodes in the current workspace |
| `superplane node describe --node ID` | Node details + GPU metrics |
| `superplane node drain --node ID` | Drain for maintenance (**moves live workloads**) |
| `superplane nodepool create --cloud CLOUD --gpus GPU:COUNT --count N` | Create a pool (**spends**) |
| `superplane nodepool list` | List pools |
| `superplane nodepool scale --pool NAME --count N` | Scale a pool (**spends**) |
| `superplane nodepool delete --pool NAME` | Delete a pool |

**Jobs:**

| Command | Description |
|---|---|
| `superplane job submit --yaml train.yaml` | Submit a training job (**spends**) |
| `superplane job list` | List jobs |
| `superplane job logs --job ID` | Stream job logs |
| `superplane job cancel --job ID` | Cancel a job |

**Cost:**

| Command | Description |
|---|---|
| `superplane cost` | Current workspace cost summary |
| `superplane cost --all` | Org-wide cost (admin) |
| `superplane cost estimate --gpus H100:2 --cloud nebius --hours 24` | Price estimate (free, read-only) |

## Decision tree

| User request | Command |
|---|---|
| "Deploy model X" | `superplane deploy --model X` (confirm cost first) |
| "What models are running?" | `superplane deploy list --json` |
| "Show me the endpoint for X" | `superplane deploy describe --name X --json` |
| "Test if X is working" | `superplane deploy test --name X` |
| "Show deployment logs" | `superplane deploy logs --name X` |
| "Undeploy X" | `superplane deploy delete --name X` (confirm) |
| "What GPUs do I have?" | `superplane node list --json` |
| "Add more GPUs" | `superplane nodepool create ...` (confirm cost) |
| "Scale up/down" | `superplane nodepool scale --pool NAME --count N` (confirm) |
| "What's my cost?" | `superplane cost` |
| "Estimate cost for H100s" | `superplane cost estimate --gpus H100:2 --cloud nebius --hours 24` |
| "Run a training job" | `superplane job submit --yaml train.yaml` (confirm) |
| "Switch workspace" | `superplane workspace use NAME` |
| "Create a workspace" | `superplane workspace create --name NAME --isolation dedicated` (confirm) |
| "Get kubectl access" | `superplane workspace kubeconfig NAME` |

## Common workflows

### Deploy a model end-to-end

1. Set workspace context: `superplane workspace use fraud-prod`
2. Check capacity exists: `superplane node list --json`
3. Estimate the cost and **get confirmation**:
   `superplane cost estimate --gpus H100:2 --cloud nebius --hours 24`
4. Deploy: `superplane deploy --model Qwen/Qwen3-Coder-Next --precision fp8`
5. Check status: `superplane deploy describe --name qwen3-coder-next --json`
6. Validate: `superplane deploy test --name qwen3-coder-next`
7. Report the endpoint URL to the user.

### Scale GPU infrastructure

1. `superplane node list --json` — what exists now
2. `superplane nodepool list --json` — what pools exist
3. Estimate the delta cost, confirm with the user, then scale or create:
   `superplane nodepool scale --pool nebius-h100 --count 5`

### Submit a training job

1. Confirm the training YAML exists (the user provides it, or you write it)
2. `superplane job submit --yaml train.yaml`
3. `superplane job list --json`, then `superplane job logs --job <id>`

### Direct Kubernetes access

```bash
superplane workspace kubeconfig fraud-prod
kubectl get nodes
```

## Common agent mistakes

| Mistake | Why it is wrong | Do this instead |
|---|---|---|
| Printing or `cat`-ing `~/.superplane/config.yaml`, or echoing `SUPERPLANE_API_KEY` | Leaks a credential into the transcript and log sink; rotation becomes the only remedy | Test auth by running `superplane workspace list --json` and checking whether it succeeded |
| Running `superplane login`, or asking the user for a token | ADP identity replaces it; an interactive login cannot complete in a pod, and a pasted token in an issue is a permanent disclosure | Stop and report the auth failure; point at `/settings/credentials` if a credential really is missing |
| `pip install superplane-cli` when the CLI is missing | A run-time install is invisible to the next run, unpinned and unreviewed | Report the image gap; the CLI belongs in the worker image build |
| Running commands without bootstrapping | Fails with a confusing error | Check `superplane --version` first |
| Forgetting workspace context | Commands fail, or target the wrong workspace | `superplane workspace use NAME`; ask the user if unset |
| Not using `--json` when parsing | Table output is fragile to parse | `--json` whenever you extract a value |
| Provisioning without an estimate | The user finds out the cost from the bill | `cost estimate` first, state the number, get confirmation |
| Deploying without checking node availability | The deploy fails confusingly | `superplane node list` first |
| Deleting a workspace without confirmation | Destructive; loses all its data | Always confirm |
| Guessing a workspace name | May target another team's workspace | `superplane workspace list` and ask |

## Error handling

| Error | Cause | Action |
|---|---|---|
| `Not authenticated` / 401 | ADP identity not wired through to the CLI | **Stop and report.** Do not attempt to log in or obtain a credential. |
| `Workspace not found` | Wrong name, or context unset | `superplane workspace list --json`; ask the user |
| `No nodes available` | No GPU capacity provisioned | Estimate cost, confirm, then create a node pool |
| `Deployment failed` | Model or resource issue | `superplane deploy logs --name X` |
| `Quota exceeded` | Org quota reached | Report to the user; do not retry in smaller increments to get under the limit |
| `Connection refused` | API unreachable | Report; likely endpoint/network wiring |

## References

- [`PORT-NOTES.md`](PORT-NOTES.md) — what changed from upstream and why
- Upstream reference docs (CLI reference, workspace management, model deployment,
  node management, troubleshooting, examples) live under
  `modules/domain-apps/ai-super-plane/reference/src/superplane-skill/references/`.
  They are **not** copied into this skill: they document the upstream CLI surface
  verbatim, including its `superplane login` / token-in-config auth model, which
  does not apply on ADP and would contradict this skill's identity rules if
  staged into the image alongside it. Consult them for CLI flag detail; treat any
  auth instruction in them as superseded by
  [Identity and credentials](#identity-and-credentials).
