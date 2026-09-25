# Agent Mail publication and deployment

These entrypoints repair the legacy standalone Agent Mail path tracked by #6113.
The only repository caller of `modules/agent-factory/scripts/build-and-push.sh`
is `deploy-agent-mail.sh`; its former relative helper path did not exist. The
actual build context is `modules/harness/mcp-hub/docker/agent-mail`. There are no
tracked Agent Mail Kubernetes manifests. Historical review documents mentioning
these scripts are not executable callers. The shared image publisher supports
four other repositories and is deliberately not reused for `mcp-agent-mail`.

Publication requires Git, Docker, AWS CLI and a pre-provisioned ECR repository
whose tag mutability is exactly `IMMUTABLE`. Set `ECR_REGISTRY` explicitly and
`AWS_REGION` to its region. The tag is the full Git commit from `ADP_SOURCE_SHA`
(default: HEAD); the Docker context comes from `git archive`, so uncommitted files
are not attributed to that commit. The script never creates repositories or
publishes `latest`. Existing immutable source tags are reused. Its sole stdout
result is the ECR digest URI; build diagnostics go to stderr.

```bash
export ECR_REGISTRY=123456789012.dkr.ecr.us-east-1.amazonaws.com
export AWS_REGION=us-east-1
bash modules/agent-factory/scripts/build-and-push.sh --dry-run
# Publication, when intended:
AGENT_MAIL_IMAGE=$(bash modules/agent-factory/scripts/build-and-push.sh)
export AGENT_MAIL_IMAGE
```

Deployment also requires Python 3 with PyYAML and kubectl. Supply an existing
manifest directory through `--manifests-dir` (or `AGENT_MAIL_MANIFESTS_DIR`). It
must contain `namespace.yaml`, `serviceaccount.yaml`, `rbac.yaml`, `pvc.yaml`,
`configmap.yaml`, `service.yaml`, `deployment.yaml`, and `ingress.yaml` unless
`--skip-ingress` is selected. Configure these for namespace `agent-mail` and
Deployment `agent-mail`, including its existing `agent-mail-auth` Secret.
Deployment container image fields must use `${AGENT_MAIL_IMAGE}`. All other
configuration must already be rendered; unresolved substitutions fail. Only the
resource kinds corresponding to those filenames are accepted, with Role and
RoleBinding permitted in `rbac.yaml`. Secret and List documents are rejected.

```bash
bash modules/harness/mcp-hub/scripts/deploy-agent-mail.sh \
  --dry-run --manifests-dir /absolute/path/to/manifests --skip-ingress
```

A dry run renders locally without AWS, Docker or Kubernetes calls, and never
changes kubeconfig or creates a StorageClass. With `--build --dry-run`, it also
checks the source publication plan. Without a supplied digest it validates the
manifests and explains that final rendering awaits publication; it does not
present a placeholder image as deployable output.

An actual apply requires `--context` (or `AGENT_MAIL_KUBE_CONTEXT`) explicitly.
It first checks that `agent-mail-auth` exists, without reading its token. Bootstrap
that Secret separately through the established secret-management procedure;
ordinary reruns neither generate nor rotate credentials. `--build` uses the
repository-absolute publisher and consumes its returned digest. No cluster or
credential changes were performed while implementing this repair.

Validation uses executable AWS/Docker/kubectl mocks and isolated Git repositories,
including uncommitted build-input tampering, missing/denied registry lookups,
mutable tag rejection, dry-run calls, digest propagation, preflight failure and
encoded Secret manifests. The suite is registered in Script Tests CI.
