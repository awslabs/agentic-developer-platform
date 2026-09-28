# Cyber app

The cyber app owns the malware-analysis persona, file-analysis pipeline, URL/domain
investigations, direct AgentCore browsing, sandbox infrastructure, images, build automation,
and operational documentation. It uses ADP's shared agent runtime, model gateway,
identity, and GitHub integration.

| Directory | Ownership |
| --- | --- |
| `agent/` | Persona, skills, investigation/report code, hosted Python dependencies |
| `workers/` | Isolated triage/static worker image, handlers, and worker tests |
| `browser/` | Legacy browser broker image retained for migration |
| `infra/` | Cyber sandbox, queues, evidence storage, IAM, and networking |
| `infra/platform-integration/` | Direct browser permissions, legacy broker resources and worker integration outputs |
| `k8s/` | Cyber worker manifests |
| `image-builder/`, `bootstrap-scripts/` | CAPE hosts and guest image builds |
| `codebuild/` | Authoritative buildspecs and build-project declarations |
| `ci/` | Composite actions implementing cyber CI, builds, deployment, and YARA ingestion |
| `environments/` | Cyber environment/backend configuration |
| `tests/` | Cross-component security/ownership regression checks |
| `releases/` | Broker compatibility release pin |
| `scripts/` | Operational entry points |
| `docs/` | Architecture, researcher workflows, and deployment records |

Start with [domain investigations](docs/domain-investigations.md),
[the GitHub team demo](docs/github-url-demo.md),
[research case output](docs/url-researcher-cases.md), or
[the architecture](docs/architecture.md). The
[ownership and migration guide](docs/component-ownership.md) explains the platform
interfaces and how existing deployments retain their resource identities.

GitHub requires workflow entry files under `.github/workflows/`. Those files
retain triggers, runner selection, permissions, concurrency, and timeouts, then
check out this repository and call an action in `ci/`. Operational steps and app
configuration live here. Platform persona registration remains in ADP's shared
catalogue and dispatch interfaces.
