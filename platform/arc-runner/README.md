# ADP Custom ARC Runner Image

Custom GitHub Actions self-hosted runner image pre-baked with every CLI tool our workflows need. Replaces the bare `ghcr.io/actions/actions-runner:2.337.0` base so workflows don't waste 3-5 minutes apt-installing `zip`/`aws`/`kubectl`/`terraform` on every run.

Adapted from [`aws-innovate/AISuperPlane/infra/arc-runner/`](https://github.com/aws-innovate/AISuperPlane/tree/main/infra/arc-runner).

## What's inside

Base: `ghcr.io/actions/actions-runner:2.337.0` (pinned to a supported release; refresh before GitHub deprecates it).

Added tooling:

- **Runtimes:** Node.js 22, Python 3.12
- **AWS:** AWS CLI v2
- **IaC:** Terraform 1.14.9
- **K8s:** kubectl 1.35.9, Helm 3.22.0 (archive checksum verified)
- **Git/GitHub:** git, gh CLI
- **Container:** Docker CLI (for ECR login/push; no DinD daemon), Kaniko executor (daemonless image builds)
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

After the image lands in ECR, update the ARC runner scale set's Helm release to point at it. In our Terraform, that's the `helm_release.arc_runner_set` resource in `modules/agent-factory/infra/` — set `template.spec.containers[0].image` to `<registry>/adp-arc-runner:<tag>`.

Then upgrade:
```
helm upgrade arc-runner-adp \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
  --namespace arc-runners \
  --reuse-values \
  --set 'template.spec.containers[0].name=runner' \
  --set "template.spec.containers[0].image=<registry>/adp-arc-runner:<tag>"
```

Once the new image is serving workflows, remove the per-workflow install steps (e.g. the "Install zip + AWS CLI + kubectl + terraform" block in `chat-agent-deploy.yml`).

## Versioning notes

- Keep the pinned runner release within GitHub's supported update window. ARC disables automatic runner updates; an expired pin can register but GitHub rejects its message requests and the pod exits. Verify the runner version, tool checks, and real job execution when updating it.
- Terraform / kubectl / Helm pins should stay in sync with what the rest of the project uses; check `modules/agent-factory/infra/` Helm releases and cluster Kubernetes version before bumping.
