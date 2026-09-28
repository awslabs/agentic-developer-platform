# Cyber component ownership and migration

Cyber-specific implementation is maintained under `modules/domain-apps/cyber/`.
The platform retains integration entry points: workflow triggers and job policy,
shared image assembly, persona registration, an explicitly enabled Terraform
module call, and the shared CodeBuild project factory. The factory reads this
app's project manifest only when cyber build jobs are selected.

## Terraform source relocation

`infra/platform-integration/` owns all eight browser-boundary resources formerly
declared in agent-factory's `url-analysis-browser-broker.tf`: the worker deny
policy, broker permissions boundary, role, role policy, service account,
Deployment, Service, and NetworkPolicy. Its outputs provide the evidence bucket,
artifact-resource list, and browser egress rule to the hosted worker's generic
configuration interfaces.

The webhook-ingress root calls this module as `module.cyber`. Eight explicit
`moved` blocks map each old resource address to `module.cyber.<old-address>`.
Retain these blocks so installations can upgrade from older releases. This is a
source/module move within the **existing webhook-ingress Terraform state**; it
is not a transfer into the sandbox state. Do not import duplicate resources or
run `terraform state rm`. Names, namespace, IAM policy contents, network rules,
service affinity, and the recorded default broker image are preserved.

Before a deployment, initialise the webhook root to discover the new module and
review a saved plan using that installation's normal backend and variable files.
The broker resources should show moves, with no create/destroy/replacement caused
by this reorganisation. Other platform drift and the protected-worker migration
hold must still be reviewed independently. No live state migration or deployment
is performed by this source change. Historical deployment records retain the
resource addresses used at the time; current broker targeting uses `module.cyber`.

The cyber sandbox keeps its existing backend key. Its configuration file moved
to `environments/dev/sandbox-backend.tfvars`; the plan/apply actions use that path.
The existing `cyber-worker` CodeBuild `for_each` key is unchanged. The shared
factory reads `codebuild/projects.json`, which now points at the app-owned worker
buildspec and declares a separate `cyber-browser` build project.

## Image ownership and compatibility

`browser/Dockerfile` builds with `modules/domain-apps/cyber` as the context:

```bash
docker build -f modules/domain-apps/cyber/browser/Dockerfile \
  -t adp-cyber-browser:local modules/domain-apps/cyber
```

The image preserves UID/GID 1001, port 8765, and the broker command path. It
connects to the managed AgentCore browser and does not install a local Chromium.
The manual **Cyber Browser Build** workflow builds and publishes it without
rolling out workloads. After reviewing/verifying the built image, supply its
immutable digest through the Cyber hosted-integration root's
`images["cyber-browser"]` setting. No broad agent-image change is needed to
release the broker.

An empty override uses `releases/browser-runtime.json`, the immutable image from
the verified September 23 deployment. This deliberately keeps the running image
stable during source relocation. It does **not** claim that the standalone image
has already been built or deployed. For installations on a different broker
image, pass that current digest as the override before reviewing the migration
plan. Keep the old image until the dedicated image has passed deployment checks.

The generic agent image still hosts cyber skills. Cyber declares their Python
dependencies in `agent/requirements.txt`; `stage-personas.sh` discovers dependency
files from all domain apps and the shared Dockerfile installs those staged files.
The platform continues to own dependencies required by its own runtime/contracts.

## Validation

The browser regression suite is in `agent/skills/url-analysis/tests`; the IAM and
relocation checks are in `tests`. Terraform mock-provider plan tests in
`infra/platform-integration/tests` check preserved identity, image, worker access,
and independent image/namespace overrides without accessing AWS.

```bash
python -m pytest modules/domain-apps/cyber/agent/skills/url-analysis/tests -q
python -m pytest --noconftest modules/domain-apps/cyber/tests -q
terraform -chdir=modules/domain-apps/cyber/infra/platform-integration init -backend=false
terraform -chdir=modules/domain-apps/cyber/infra/platform-integration test
```

Install the browser fixture requirements and Playwright Chromium before running
the browser suite. The GitHub workflow entrypoints and the expanded composite
steps must also pass actionlint; app implementation paths are included in the
corresponding workflow filters.

Source-relocation validation (23 September 2026): 256 URL/browser tests, nine
boundary/worker-configuration tests, six image-staging tests, and both Terraform
mock plans passed. The module and webhook root validate. The eight broker
resource bodies match their previous definitions after substituting module
inputs. Cyber workflow wrappers and all nine expanded composite actions pass
actionlint; moved documentation links resolve. Docker is unavailable in the
local validation environment, so the standalone image was not built or deployed.

The shared `webhook-ingress-deploy.yml` has a pre-existing empty choice that
current actionlint rejects; the same error reproduces from the unchanged base.
Only its path filters change here. This baseline issue is tracked as `adp-3a1`.
