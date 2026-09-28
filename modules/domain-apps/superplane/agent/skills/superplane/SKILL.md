---
name: superplane
description: >-
  Use to inspect Superplane workspaces, provider readiness, workload operations
  and costs through the maintained ADP CLI. For GPU machine selection and the
  issue-driven SkyPilot-to-EKS workflow, use the sibling skypilot skill.
metadata:
  domain: superplane
  authentication: existing ADP identity
---

# Superplane on ADP

Use the installed `adp superplane` CLI and the current ADP run's identity. There
is no standalone `superplane login`, separate token store or runtime package
installation. A missing CLI, denied request or unavailable route is a concrete
runtime gap to report; do not obtain alternate credentials or call an unscoped
provider API to work around it. Never read or print credential files or ask for
keys in an issue.

For an issue requesting GPU capacity or a workload, read the sibling `skypilot`
skill. SkyPilot owns machine selection/provisioning; the workspace EKS hosts the
requested Kubernetes workload. The UI is an optional view of the same operations.

## Discover the current target

Use the workspace named by the issue or current run binding. If it cannot be
resolved unambiguously, ask which workspace is intended before any mutation.

```sh
adp superplane workspace list --json
adp superplane workspace describe --workspace WORKSPACE --json
adp superplane onboarding readiness --workspace WORKSPACE --json
adp superplane node --workspace WORKSPACE --json
adp superplane quota show --workspace WORKSPACE --json
adp superplane cost --workspace WORKSPACE --json
```

These are the maintained CLI shapes: `node` directly lists nodes. Do not invent
`node list`, `nodepool`, `job submit`, `deploy test`, or `cost estimate` commands
from the old upstream skill. Where a required operation lacks an installed CLI
or tool route, report the missing capability instead of claiming it ran.

Use JSON output for observations. Check source and freshness: fixture offers are
not live availability, and a known-rate estimate is not a provider bill. A null
or incomplete cost is unknown rather than zero.

## Governed serving operations

The current CLI provides `deploy preview`, `deploy create`, `deploy list`,
`deploy teardown-preview` and `deploy delete`. Use `adp superplane deploy VERB
--help` for the exact installed arguments. Preview/create require a workspace
profile, model settings and an operation UUID. Creation additionally requires the
exact preview revision and its approved request ID. An agent cannot approve its
own request by passing `--yes`; that flag only suppresses a CLI prompt.

Keep the same operation UUID, plan revision and approval across retries. Recover
an uncertain response through `adp superplane onboarding operation show` or
`operation recover` using the original receipt. Do not create a second operation
to recover the first. Teardown names the original deployment UUID and uses its
own reviewed operation; successful submission does not prove resource absence.

Honor the issue's existing authorization and requested retain/cleanup outcome.
Prepare a concrete request before seeking any additional decision required by
the backend. Do not request confirmation again merely because the agent reached
another step within already authorized scope.

## Current limits

The controller currently launches AWS native EKS nodes using installed profiles.
The SkyPilot skill restores resource planning, but external-node networking/join,
provider credentials and shared serving must be installed before a mixed-provider
request can execute. Report the actual capability boundary to the issue. Source
files, Ready nodes and a successful batch task do not establish authenticated
serving or complete hybrid acceptance.
