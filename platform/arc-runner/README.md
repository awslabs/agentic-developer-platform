# ADP Custom ARC Runner Image

Custom GitHub Actions self-hosted runner image pre-baked with every CLI tool our workflows need. Replaces the bare `ghcr.io/actions/actions-runner:2.333.1` base so workflows don't waste 3-5 minutes apt-installing `zip`/`aws`/`kubectl`/`terraform` on every run.

Adapted from [`aws-innovate/AISuperPlane/infra/arc-runner/`](https://github.com/aws-innovate/AISuperPlane/tree/main/infra/arc-runner).

## What's inside

Base: `ghcr.io/actions/actions-runner:2.333.1` (pinned — session-context notes `:latest` can get flagged "deprecated" and exit-7 loop).

Added tooling:

- **Runtimes:** Node.js 22, Python 3.12
- **AWS:** AWS CLI v2
- **IaC:** Terraform 1.14.2
- **K8s:** kubectl 1.35, Helm 3.17
- **Git/GitHub:** git, gh CLI
- **Container:** Docker CLI (for ECR login/push; no DinD daemon), Kaniko executor (daemonless image builds)
- **Utilities:** zip, unzip, jq, curl, wget, sudo

## Build + push (CI)

The `.github/workflows/arc-runner-build.yml` workflow fires on pushes to this directory. It packages the repo, starts a CodeBuild job with `codebuild/bs-arc-runner.yml`, and pushes to ECR:

```
<account>.dkr.ecr.us-east-1.amazonaws.com/adp-arc-runner:<sha>
<account>.dkr.ecr.us-east-1.amazonaws.com/adp-arc-runner:latest
```

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

- **Do NOT upgrade the `actions-runner` base past `2.333.1`** without re-checking the session-context notes (the ARC chart is also pinned at `0.13.1` for a related bug).
- Terraform / kubectl / Helm pins should stay in sync with what the rest of the project uses; check `modules/agent-factory/infra/` Helm releases and cluster Kubernetes version before bumping.
