# ADP Custom ARC Runner Image

Custom GitHub Actions self-hosted runner image pre-baked with every CLI tool our workflows need. Replaces the bare `ghcr.io/actions/actions-runner:2.337.0` base so workflows don't waste 3-5 minutes apt-installing `zip`/`aws`/`kubectl`/`terraform` on every run.

Adapted from [`aws-innovate/AISuperPlane/infra/arc-runner/`](https://github.com/aws-innovate/AISuperPlane/tree/main/infra/arc-runner).

## What's inside

Base: `ghcr.io/actions/actions-runner:2.337.0` (pinned to a supported release; refresh before GitHub deprecates it).

Added tooling:

- **Runtimes:** Node.js 22, Python 3.12
- **AWS:** AWS CLI v2
- **IaC:** Terraform 1.14.9 (ADP security rebuild with Go 1.26.8 and pinned dependencies)
- **K8s:** kubectl 1.35.9, Helm 3.22.0 (archive checksum verified)
- **Git/GitHub:** git, gh CLI
- **Container:** Docker CLI (for ECR login/push; no DinD daemon), Kaniko 1.28.5 (ADP security rebuild; daemonless image builds)
- **Utilities:** zip, unzip, jq, curl, wget, sudo

## Build + push (CI)

The `.github/workflows/arc-runner-build.yml` workflow fires on pushes to this directory. It packages the repo, starts a CodeBuild job with `codebuild/bs-arc-runner.yml`, and pushes to ECR:

```
<account>.dkr.ecr.us-east-1.amazonaws.com/adp-arc-runner:<sha>
<account>.dkr.ecr.us-east-1.amazonaws.com/adp-arc-runner:latest
```

Candidate builds can set `PUBLISH_LATEST=false` to publish only the exact source tag. Verify that image before updating the scale set or the shared `latest` tag.

For the September 24 dev recovery, `RUNNER_DOCKERFILE=Dockerfile.recovery`
refreshes the runner files on the exact previously deployed image in account
879318057152. This preserves the installed tools while rolling Ubuntu mirrors
return package 404s. It is an account-specific recovery input, not the default
full rebuild. Both paths check the runner version and tools before publishing.

## Rollout

After verification, pin `runner_image_tag` in
`modules/agent-factory/infra/variables.tf` to the published tag and digest.
The `runner_image` full URI override takes precedence when configured.

The existing main-only `Agent Factory Infra Apply` workflow accepts a `target`
input. The org pool address is
`module.arc_runner[0].helm_release.arc_runner_set`; the optional separate agent
pool is `module.agent_workflow_pool[0].helm_release.agent_workflow[0]`. Review the
saved plan, including dependencies, before applying it. This workflow also runs
its existing GitHub secret preflight, so it is not a Helm-only operation.

For an authorized direct Helm maintenance operation, preserve the complete
existing values and change only the named runner's image. The releases declared
here are `arc-runner-org` and `arc-runner-agent`, with chart version `0.14.2`.
Confirm the live release names before selecting either. Retain each pool's
service account, command, environment and resource settings. New runners use
the new image; let existing jobs finish before removing their old runners.

## Versioning notes

- Keep the pinned runner release within GitHub's supported update window. ARC disables automatic runner updates; an expired pin can register but GitHub rejects its message requests and the pod exits. Verify the runner version, tool checks, and real job execution when updating it.
- Terraform / kubectl / Helm pins should stay in sync with what the rest of the project uses; check `modules/agent-factory/infra/` Helm releases and cluster Kubernetes version before bumping.

## Terraform security rebuild

The builder checks out the exact Terraform 1.14.9 commit and applies
`terraform-security-dependencies.patch` to its module files. The runtime/compiler
and dependency updates address the embedded Go, SSH, NTLM, gRPC and telemetry
advisories without changing the Terraform release or application source. The
build uses a digest-pinned Go image, verifies modules, and forbids module-file
changes during compilation. Upstream licensing and a modification notice ship
with the binary under `/usr/local/share/licenses/terraform/`.

Refresh the patch from the pinned upstream commit when updating dependencies;
review the complete resolved graph and run isolated lifecycle and upstream
compatibility tests before publishing. Runner rollout is a separate operation.

## Kaniko security rebuild

The maintained `osscontainertools/kaniko` v1.28.5 release supplies credential
helpers and certificates from its digest-pinned image. A separate Go 1.26.8
builder checks out the exact upstream commit and applies
`kaniko-security-dependencies.patch`, updating x/crypto to 0.57.0 and its
resolved dependencies. Module verification and checksum guards prevent
compilation from silently changing the reviewed graph. The rebuilt executor
ships with the upstream license and an ADP modification notice.

Telemetry remains disabled unless explicitly configured through Kaniko's
telemetry endpoint setting. Image publication and runner rollout require
separate acceptance; a local build does not establish deployed remediation.

## Bundled runner npm security updates

The runner retains its bundled Node 20 and Node 24 executables. Their npm
installations use npm 11.20.0, which supports both installed Node versions.
Checksum-verified upstream package archives include tar 7.5.22 and patched
transitive dependencies. Node 20 moves from npm 10 to npm 11; package packing,
installation, execution and clean installation are tested under each runtime. The Docker build exercises tar's default decompression-ratio limit
with an 8 MiB synthetic fixture and verifies a valid archive still extracts.
This bounded check does not exhaust disk or require external services.

Validate offline package packing, installation, execution and clean installation
under each bundled Node runtime before publishing a changed npm package.

### September 2026 High security refresh

The runner opts into the upstream `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true`
transition supported by runner 2.337.0. Both former `node20` runtime directories
are compatibility links to their Node 24 counterparts; this also updates the
runner's internal `hashFiles` executable path. Node 20 is no longer available
in this image. Workflows that depend on Node 20-specific behavior must migrate.
The global Node 22 CLI remains available and uses npm 11.20.0.

`security/high-tools/test-node-actions.py` exercises the actual Node 20 action
bundles from checkout v4 and github-script v7, a local HTTP Git checkout,
runner `hashFiles`, and npm install/exec. Fixture source commits are recorded in
`security/high-tools/source-lock.json`. These local checks do not substitute
for a canary workflow before runner rollout.

The image keeps Docker daemon, containerd, runc, Buildx, Kaniko, Helm, Terraform,
and credential helpers. It updates Docker to 29.8.1 and containerd to 2.4.1,
rebuilds Buildx 0.37.1, runc 1.4.3, Helm 3.22.0 and the ACR helper with fixed
Go dependencies, and builds AWS CLI 2.37.4 with Python 3.14.7 and statically linked Expat 2.8.5. The exact Python XML adapters
are supplied by a small immutable build artifact; ordinary valid/invalid XML
is tested through the real AWS CLI against a local HTTP fixture. Source commits,
dependency patches and download hashes are committed next to the Dockerfile.
The AWS CLI portable executable is an ADP source build, not an AWS-signed ZIP.
