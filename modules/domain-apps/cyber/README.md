# Cyber app

The cyber app owns the malware-analysis persona, file-analysis pipeline, URL/domain
investigations, direct AgentCore browsing, sandbox infrastructure, images, build automation,
and operational documentation. It uses ADP's shared agent runtime, model gateway,
identity, and GitHub integration.

The basic platform deployment does not install cyber resources. Deploy the
cyber sandbox through `scripts/deploy.sh` when the app is needed. Select its
CodeBuild jobs from the Cyber Terraform root through `scripts/deploy.sh`.
That script publishes the Cyber gateway broker settings from Cyber state. Run
`scripts/configure-gateway.sh` after a base platform upgrade if the Cyber
broker is installed; the base gateway template contains no Cyber settings.
Install the optional hosted worker integration through
`scripts/deploy-hosted-integration.sh --apply <reviewed.tfvars>` to create its
resources, including the Cyber hosted-worker ECR repository. Set
`CYBER_WORKER_BASE_IMAGE` to the deployed base worker's immutable digest and run
`scripts/publish-hosted-worker.sh`. Put its `worker_image_digest` output in the
reviewed Cyber tfvars and apply the hosted integration again. Then select
`enabled_domain_integrations = ["cyber"]` in the webhook update settings and
add the Cyber image digest to `agent_authority_worker_image_digests` when protected
worker authority is enabled. The webhook stack reads the Cyber image and settings
from Cyber state; the base worker image contains no Cyber skills or tool adapters.
The hosted resources live in Cyber's own state; the webhook stack only reads
its worker configuration. Older installations must migrate their legacy
`module.cyber` resources from webhook state before using the new script.

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
