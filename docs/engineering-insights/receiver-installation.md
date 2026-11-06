# Install the L5 Insights contribution receiver

The owning module now has a source-image publisher and protected receiver
configuration reads. This installs the contribution receiver described in
[consumer receivers](../l5-services/consumer-receivers.md); it does not establish
live L5 qualification or the broader proposed Insights analytics capability.
New emitter and distiller Deployments retain zero replicas. Installation must
not be used as evidence that native contributions were accepted.

Follow the [agent deployment procedure](../adp-platform-deployment/deploy-with-agent.md)
with the confirmed account, region and existing scoped roles. The build uses a
local Docker engine or an explicitly selected, authenticated SSH Docker engine.
Keep target values, receipts and plan evidence outside the checkout in a private
directory.

## Installation order

1. Review the exact source revision and the existing deployment role/workload
   boundary inventory. The module requires both admitted workload roles and
   their exact permissions boundaries. Use `deploy.sh --bootstrap-ci` to inspect
   its policy requirements; that option only prints a policy.
2. If this module has no resources, review and apply `--infra` without an image
   first. This creates the existing module-owned ECR repository and infrastructure
   without trying to resolve an unpublished image. Preserve current Terraform
   state and installed images during upgrades.
3. Publish the reviewed emitter source to that repository with the script below.
4. Populate or verify the two protected receiver parameters described below.
5. Review `--emitter --plan` with the receipt's digest, then apply that exact image
   through the module entrypoint or protected Insights deployment workflow.
   Review an actual saved plan where the deployment procedure requires one.
6. Independently verify installed configuration and zero replicas. Complete the
   selected release's live acceptance and explicit activation gates separately.

## Publish the reviewed source image

Use a clean checkout at the reviewed 40-character commit and an already selected
AWS role. The script checks STS against the explicit account and role, the
protected platform account parameter and the existing immutable ECR repository.
It does not assume or modify a role. The selected role must already permit the
scoped ECR upload operations, image readback and ECR login authorization.

```bash
python3 modules/insights/scripts/publish-emitter-image.py \
  --environment "$ADP_ENVIRONMENT" --region "$AWS_REGION" \
  --account "$ADP_ACCOUNT_ID" --role-arn "$INSIGHTS_PUBLISH_ROLE_ARN" \
  --source-revision "$REVIEWED_SOURCE_REVISION" \
  --output "$PRIVATE_RELEASE_DIRECTORY/insights-emitter-image.json"
```

`PRIVATE_RELEASE_DIRECTORY` must exist with mode `0700`, outside the checkout.
The script archives the emitter Dockerfile, requirements, server and common
Python library from that commit. The archive retains repository-root paths;
working files, ignored credentials and unrelated repository content cannot enter
the build context. Docker builds and loads one `linux/amd64` image. A temporary
private Docker configuration keeps login credentials separate from the operator's
normal configuration and is removed afterward.

For an existing remote Docker engine, keep the AWS CLI and ECR credentials on
the local publishing machine. Install the local Docker CLI, its Buildx plugin
and Skopeo, verify the engine's existing SSH identity, then select the archive
transport:

```bash
DOCKER_HOST="$REVIEWED_DOCKER_HOST" \
python3 modules/insights/scripts/publish-emitter-image.py \
  --environment "$ADP_ENVIRONMENT" --region "$AWS_REGION" \
  --account "$ADP_ACCOUNT_ID" --role-arn "$INSIGHTS_PUBLISH_ROLE_ARN" \
  --source-revision "$REVIEWED_SOURCE_REVISION" \
  --push-transport archive \
  --docker-cli-plugins-dir "$DOCKER_CLI_PLUGINS_DIR" \
  --output "$PRIVATE_RELEASE_DIRECTORY/insights-emitter-image.json"
```

`REVIEWED_DOCKER_HOST` must be an SSH Docker endpoint such as `ssh://build-host.example`;
unset `DOCKER_CONTEXT` so it cannot override the selected engine. The optional
plugin directory must be an existing absolute local path. For Homebrew that is
commonly `/opt/homebrew/lib/docker/cli-plugins`. Only this plugin setting is
written into the fresh private Docker configuration; existing user configuration
and registry credentials are never copied.

Archive transport sends only the reviewed Git archive to the builder. Docker/SSH
subprocesses receive a small environment containing the selected endpoint and
local SSH authentication references, without AWS, registry or GitHub credentials.
The inspected image ID is streamed through `docker image save` into a temporary
local file with mode `0600`. Older Docker engines identify images by their config
digest. Containerd-backed engines can identify the image by a manifest digest;
the publisher verifies that exact manifest's raw hash and size, then its config
and layer digests against the archive. Index or ambiguous descriptors are refused.
Both paths establish the actual config digest before ECR authorization is
requested. Local Skopeo receives the ECR
password through stdin and writes a separate private auth file, then copies the
archive into the selected ECR repository. Docker never receives that password
or runs a registry login/push in this mode. Temporary archive and authentication
files are removed afterward; the source image remains in the selected builder.
The default Docker publication path accepts only a local engine.

The source tag is immutable. After pushing, independent ECR readback must prove
the manifest digest and the locally inspected image config digest. The new
private receipt records the source/context/config digests, target and immutable
`repository@sha256:...` image reference. Its `qualified` field remains `false`.
Use that digest for the emitter stage; never substitute `latest` or an unrelated
preexisting tag.

A fsynced `.pending.json` is written before push. If the response is lost, rerun
with the same arguments and output path: the script reconciles the remote image
without rebuilding or pushing. A source tag without its original receipt or
pending record is refused. This reconciliation also covers lost Skopeo copy
responses without another build, credential request or upload. If a pending
image is still absent, preserve the journal and `.build.log`, investigate the
failed push, and select a distinct
receipt for an authorized rebuild. Existing records are never overwritten.

## Protected receiver parameters

These selected-account, selected-region SSM **String** parameters are required
whenever an emitter image is installed or preserved:

| Parameter | Value |
|---|---|
| `/adp/<environment>/insights/l5-consumer-verification-keys` | Nonempty JSON map of key IDs to public Ed25519 SPKI PEM values |
| `/adp/<environment>/insights/l5-authority-gateway-url` | Current Gateway authority origin |

The public map comes from the installed Gateway's public verification-key
ConfigMap (`adp-control-verification-keys`, namespace `adp-agents`, `keys.json`).
Copy only the public map through the authorized configuration path; do not read
or distribute signing Secrets. Preserve the installed key IDs and all current
verification keys, including overlap required during rotation.

The standard private Gateway Service origin is
`http://bedrockgateway.adp-gateway.svc.cluster.local:80`. Port 8080 is the Gateway
container port, not this Service's port. A selected HTTPS origin is also accepted;
credentials, paths, queries, fragments, whitespace and control characters are
rejected. Use the observed Service configuration, not an inferred pod endpoint.

Populate absent values using the authorized operator configuration process.
For existing values, compare against the installed configuration and preserve
operator settings; do not replace unreadable values with defaults. The deployment
identity needs `ssm:GetParameter` for these two exact parameter ARNs in addition
to its existing platform account read. The bootstrap policy helper includes those
reads and does not grant parameter writes or broaden IAM role admission.

`deploy.sh` reads and validates both values into a private temporary Terraform
variable file before planning any selected or retained emitter. Denied reads,
missing values, an empty map, private keys or malformed origins stop the stage
before plan/apply. Initial infrastructure with no emitter does not need these
parameters. Installing a sibling distiller preserves the emitter image and
reloads its protected receiver settings. Temporary variable and plan files are
removed when the stage exits.
