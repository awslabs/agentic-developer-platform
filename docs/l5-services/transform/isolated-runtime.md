# Transform isolated runtime assembly

## What this change provides

The Transform worker controls short Kubernetes operations directly. There is no
new control Lambda, public function URL, HTTP result server, copied kubeconfig,
or Task-shaped validation identity. Its IAM identity is mapped to a dedicated
Kubernetes group; SQL work, attempt and lease checks still govern every provider
operation. Long-running provider work and repository checks execute in separate
Pods, not in Lambda.

This is implementation assembly, not installed qualification. Keep consumers
disabled until the final shared integration, Gateway backend and native
qualification are complete. Offline tests use synthetic external observations.

## Provider and check images

`modules/L5-services/executor-image/transform/Dockerfile` takes an approved
Python, Node.js (22 or newer), and Git base image pinned by digest, the built L5 wheel
as `worker.whl`, and the verified Transform 3.19.0 vendor bundle as `atx.zip`.
The bundle SHA-256 is
`2538a09e24ab5a8b2abeef16fec824600d66b5b215dceccd11e5c4f4318e904e`.
The executable is a Node.js program with adjacent runtime chunks, not a standalone
ELF binary. The image retains the entire bundle and explicitly runs it through
`/usr/local/bin/node`. The image build rejects any
binary other than SHA-256
`87117a33f1aab02e78e0ed216549edef49918329df55e756bebd84d8f79a10bf`.
Publish the resulting image by digest to
`adp-${environment}-l5-transform-executor`. Do not commit the executable or
private build inputs.

`Dockerfile.checks` uses a separately approved test image, the same worker wheel,
and `check-policy.json`. The policy contains the selected `bundle_sha256`,
`lock_sha256`, `oracle_version`, and exact `commands` records. Its dependencies
must already be installed in the pinned base: check Pods have no network access.
This runner recognizes pytest's passing-test summary and Node's complete TAP
summary when the command explicitly selects `--test-reporter=tap`. Other test-output formats
remain inconclusive rather than inventing a test count. Kubernetes establishes
the exit code, timestamps and Pod UID. Its combined stdout/stderr log is retained
once and explicitly identified as combined in the receipt.
Large check inputs upload across bounded continuations using a scoped,
resource-version-fenced progress record. Already confirmed chunks are not read
again on every continuation. Before executing tests, the check image verifies
the complete mounted input against the digest fixed in the Pod command.

### Release image publication

The shared release workflow calls
`modules/L5-services/scripts/release-transform-images.py`. It builds these two
Pod images in the single bootstrap-owned repository; the legacy generic
validation Lambda publisher is not part of Transform's release.

Set the protected environment secret `ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON` to a
private version-1 JSON object with these fields:

| Field | Required input |
| --- | --- |
| `schema_version` | `1` |
| `provider_base_image` | Approved digest-pinned Python, Node.js 22+, Git image |
| `check_base_image` | Approved digest-pinned Python image containing the selected test dependencies |
| `check_policy` | Exactly `bundle_sha256`, `lock_sha256`, `oracle_version`, and `commands` from the selected test policy |

Each command contains `argv`, repository-relative `cwd`, `environment`,
`timeout_seconds` (1–120), and positive `minimum_test_count`. Provider/platform
credentials are prohibited in that environment. The image's own digest and
Product policy reference are added to qualification after publication; including
them in the image build policy would create a circular identity.

The workflow validates this configuration before package publication. It passes
the exact published wheel to minimal temporary build contexts, verifies both
vendor hashes, and checks the selected AWS role, account, region and immutable
repository. Provider and check tags bind the source, wheel, Dockerfile, base and
respective vendor/policy inputs. A retry reads an existing immutable image and
verifies its build labels, platform, entry point and non-root user before reusing
it. Registry read errors are not treated as a missing image. After a new push, the
registry manifest must match the local build's manifest or configuration digest
(Docker reports one or the other depending on its image store).

The private artifact handoff retains the compatibility filename
`validation-images.json`; its `images.provider` and `images.checks` fields contain
actual ECR digest references. This establishes publication only. Bootstrap,
node isolation, Product policy registration and native acceptance remain separate
requirements. The publisher neither creates repositories nor enables consumers.

For a remote Docker daemon, select `L5_TRANSFORM_IMAGE_TRANSPORT=archive`.
The publisher refuses the ordinary Docker transport when `DOCKER_HOST` selects
SSH or TCP. Archive mode uses local Skopeo for registry authentication, reads
each pinned single-platform base into a private local archive, verifies its
config and layers, then streams only image bytes to Docker. The build receives
a verified local base alias; the retained build inputs still name the original
immutable registry reference. Docker and SSH inherit an allowlisted environment
without AWS, GitHub, registry, proxy or arbitrary operator secrets. Their
temporary Docker configuration contains no credential helpers or registry auth.

Completed images are exported by inspected immutable ID. Before upload the
publisher verifies both classic Docker config IDs and Docker 29/containerd
manifest IDs, including compressed layer hashes and uncompressed diff IDs.
Only local Skopeo receives the ECR token and uploads the archive; independent
ECR readback must match the verified config digest. Repeated runs import and
verify the existing immutable image instead of replacing its tag. The Skopeo
auth file and archives remain in the private temporary directory until cleanup.
`L5_TRANSFORM_DOCKER_CLI_PLUGINS_DIR` may select an explicit installed Buildx
plugin directory without copying the operator's Docker configuration.

The provider uses `atx json` with tool approval required. The mediator rejects
shell/network tools, paths outside the projection, links, executable writes and
unknown control frames. Landlock ABI 3 or newer restricts filesystem access and
execution in the provider child; there is no unrestricted fallback. Only the
provider Pod receives the short-lived Product-scoped AWS session. Check Pods
receive neither that session nor a service-account token.

## Operator-only bootstrap

The separate root `modules/L5-services/infra/transform/bootstrap/` owns the ECR
repository, artifact bucket, worker EKS access entry, initial namespaces,
namespaced RBAC, network policies and resource quotas. Use the approved separate
Transform-artifacts state key. It is an operator action, not part of the normal
release role. The normal service root remains `infra/transform/` and receives no
permission to create EKS access entries or elevate its Kubernetes permissions.

Bootstrap is deliberately staged without placeholder images:

1. With the selected operator AWS authority and approved Transform-artifacts
   state, apply the bootstrap root with `executor = null` (the default). Supply
   the existing worker role ARN/name and normal account/region/environment
   inputs. This creates only the immutable ECR repository: no EKS lookup, IAM
   policy, bucket, node access, namespace or runtime environment is created.
2. The coordinator builds and publishes the real provider and check images,
   verifies their contents and records the actual immutable image digests.
   Invented digest strings or placeholder images must not substitute for builds.
3. With separately authorized Kubernetes bootstrap access, apply this same root
   and state using protected `executor` inputs and those actual image digests.
   Independently qualify the selected new node; do not reuse retired-node
   evidence. Only then install the worker using the resulting environment.

If the selected cluster has no dedicated candidate capacity, this same bootstrap
root can first prepare `isolated_node_pool` while keeping `executor = null`. Its
inputs are `cluster_name`, the existing Auto Mode `node_role_name`, explicit
private `subnet_ids` and reviewed `security_group_ids`. This separate
`adp-${environment}-l5-transform` NodeClass/NodePool leaves the Codex validation
pool unchanged. It requires the installed Auto Mode CRD to support
`advancedCompute.kubelet.podPidsLimit` and the node role's existing Auto Mode access
entry. The class enforces `DefaultDeny` network policy, logs policy events and
limits each Pod to 128 PIDs. The pool admits only on-demand Linux/AMD64
`m6a.xlarge` instances, caps total CPU at four, and uses the
`adp.dev/transform=only:NoSchedule` taint. Nodes receive only
`adp.dev/transform-candidate=v1`; they never inherit an isolation qualification.

The pool starts with zero nodes. An operator probe Pod selecting the candidate
label and tolerating its taint can provision capacity. The four-vCPU pool limit
targets one node, but Auto Mode limits are eventually consistent: verify actual
node count and cost exposure before continuing. The optional
`isolated_node_pool.capacity_mode` defaults to `candidate`, which consolidates
empty nodes after 60 seconds. Select `warm` explicitly in the reviewed bootstrap
inputs to disable idle consolidation (`consolidateAfter = "Never"`) for an
installed service. Warm mode retains idle capacity and its compute/storage costs;
it does not create a fixed replica count or extend the 24-hour node lifetime.
Both modes retain the same instance, capacity limit, taint and security controls.
Neither requires an indefinite hold Pod or permanent `do-not-disrupt` annotation.

Probe, pin and qualify the exact new node UID before using it in `executor`.
Node expiry, failure or other replacement still requires the executor operator
to independently qualify the new node, apply its reviewed `executor` inputs to
update node-observer RBAC, and reinstall the protected worker node name/UID before
resuming work. Replacement nodes never inherit qualification. Automatic
replacement qualification is not implemented; warm mode prevents the current
node from disappearing solely because the service is idle. Capacity preparation
creates no worker EKS access, artifact permissions,
provider namespace or runtime environment. Keep the same non-null pool input in
the later executor plan so Terraform retains that capacity owner.

The optional `provider_endpoint` stage creates the AWS-owned
`com.amazonaws.${region}.transform-custom` interface endpoint with private DNS initially disabled,
plus a dedicated node identity security group and endpoint security group. Supply
the selected `vpc_id` and private `subnet_ids`. Endpoint ingress permits TCP 443
only from that dedicated node group, which the NodeClass attaches beside the
existing cluster node group. No public CIDR ingress is added. Before planning,
verify the regional service's owner, private DNS name, supported endpoint policy,
availability zones and absence of a conflicting private DNS endpoint.

Omit `authorizations` during bootstrap: the endpoint policy then denies every
provider API call. Existing VPC DNS resolution remains unchanged. TLS/network
canaries connect to the observed endpoint ENI addresses using the validated
regional provider TLS server name; they establish no provider authorization or
native acceptance. A later reviewed `authorizations` list names exact provider
`role_arn` and `package_arns` values from current Product-owned bindings. Only
`GetTransformationPackageUrl` and `ExecuteTransformation` on those packages and
the documented resource-less `ConverseStream` action are admitted, with the
selected region condition. Broker sessions still impose the live work lease and
source authority; endpoint policy does not create that authority. Keep
`private_dns_enabled=false` throughout deployment. The provider Pod uses the
protected installation endpoint reference, leaving other VPC workloads' DNS
resolution unchanged.

Read the endpoint's actual ENIs after apply, verify their VPC/subnet/private-IP
ownership and endpoint-specific DNS resolution from the candidate node, then supply those
IPv4 `/32` addresses as `executor.provider_cidrs`. Never substitute public DNS
snapshots, VPC-wide networks or example addresses. Endpoint replacement requires
fresh readback and allowlist review. Preserve both optional stage inputs when
planning the later executor resources. Interface endpoint AZ-hours and data
processing are billable even before a node is provisioned; report that exposure
separately from on-demand EC2, EKS Auto Mode and node volume costs.

The coordinator owns these builds and applies. Executor bootstrap requires the
existing Gateway role name in the top-level `gateway_role_name` input, plus the
following protected `executor` inputs:

| Input | Meaning |
| --- | --- |
| `cluster_name` | Selected private EKS cluster |
| `node_name`, `node_uid` | Independently qualified isolation node; the root does not assign a qualification label |
| `provider_namespace`, `check_namespace` | Separate restricted namespaces |
| `provider_image`, `check_image` | Immutable image references, including `@sha256:` digests |
| `provider_cidrs` | Narrow provider endpoint networks permitted on TCP 443; not unrestricted internet egress |

The worker group is `adp-${environment}-l5-transform-control`. It can read only
the selected node at cluster scope. In its two namespaces it can create/read/
patch/delete owned Pods and ConfigMaps and read Pod logs and the deny policy.
Only the provider namespace grants Secret access. Each namespace is capped at
five Pods. Both use the unannotated `l5-transform-sandbox` service account with
token automount disabled. The selected cluster must enforce NetworkPolicy; node qualification
must also verify Landlock, metadata isolation, image pulls and private worker
connectivity. Deployment tooling must not obtain cluster-admin by assuming that
an IAM grant implies Kubernetes authorization.

The coordinator owns the bootstrap-role policy integration and selected live
inputs. The bootstrap role needs the selected worker's EKS access-entry operation,
the named ECR repository and artifact resources, and reviewed cluster bootstrap
authority. **Do not grant those operations to the normal release role.**

## Normal release inputs

Copy the bootstrap's `worker_environment` output into protected
`services.transform.worker_environment`, preserving the shared Gateway/RDS/queue
settings. Set `services.transform.worker_factory` to
`l5_services.runtime_transform:factory`. No new shared release-schema field is
needed. The service-specific environment keys are:

- `L5_TRANSFORM_CLUSTER_NAME`, `L5_TRANSFORM_CLUSTER_ENDPOINT`, `L5_TRANSFORM_CLUSTER_CA`
- `L5_TRANSFORM_NODE_NAME`, `L5_TRANSFORM_NODE_UID`
- `L5_TRANSFORM_PROVIDER_NAMESPACE`, `L5_TRANSFORM_CHECK_NAMESPACE`
- `L5_TRANSFORM_PROVIDER_IMAGE`, `L5_TRANSFORM_CHECK_IMAGE`
- `L5_TRANSFORM_ARTIFACT_BUCKET`

When the bootstrap owns a private provider endpoint, its environment output also
includes `L5_TRANSFORM_PROVIDER_ENDPOINT_REF`. This JSON reference binds the
actual endpoint ID, VPC, service name, regional endpoint DNS name and installation
account. The runtime checks it against the immutable provider image's ECR account
and region. Public hosts, another service/region/account, credentials, ports,
paths, queries and fragments are rejected. Only the provider Pod receives it;
repository check Pods receive neither that reference nor a vendor endpoint
environment variable. The trusted provider entry point translates the reference
to the pinned bundle's `ATXCT_FES_ENDPOINT`, while still discarding inherited
vendor overrides and proxy variables. TLS verification and the existing regional
SigV4 provider session remain in use. Product settings, source content, goals and
request payloads cannot select this endpoint.

The shared runtime also supplies `AWS_REGION`, authenticated Gateway transport,
RDS sessions, scoped Product source readers, the source archive and queue
publisher. Development integration uses the shared runtime module at
`a0242e97021207cd340311dbcd25ad5f26db3951`; revalidate against its final merged
revision before release. Do not copy a developer's AWS profile or cluster token
into these settings.

The pinned bundle's programmatic handler accepts `transformationVersion`,
`trustAllTools`, `interactiveMode`, and `agentMinutesLimit`. Its emitted frames
include tool-start/result/output events as well as permission requests, session
identity, usage and completion. Informational events never establish successful
execution; unknown control frames and trusted tools outside the file policy fail
closed.
An `input_required` frame is inconclusive, even after partial edits. The finite
runner stops locally without publishing a completed patch; only a native
`complete` frame with `status: completed`, session identity and usage can establish
successful completion.

## Original-session continuation

The pinned `atx` 3.19.0 NDJSON interface continues an existing session using
`start_session` with `conversation_id` and its original local metadata. The
provider runner can continue one exited CLI child within the same Pod. It never
selects a latest session, creates replacement metadata or launches another Pod.

Continuation requires the original native ID, last observed checkpoint, source
directory, baseline commit, region and a retained cumulative minute count at
least as large as the last observed count. The original absolute deadline,
credential session and total minute allowance remain unchanged. The runner
reaps the first process group before continuing; a second exit, missing or
changed metadata, partial frame, explicit provider failure, clarification or
expired deadline cannot start another child. It requires an exact
`resume_status` acknowledgement and fresh monotonic usage before completion.
Qualified checkpoint IDs must carry the original session prefix; a foreign
prefix or repeated start frame is rejected before continuation.
Stored vendor configuration cannot import another tool or identity setting.

Completed provider checkpoints retain a bounded `transform-provider-trace/v1`
record: original session/checkpoint transitions, cumulative usage, one
continuation metadata digest, and actual tool-mediation decisions. The record
omits provider text, file contents, arbitrary command strings and credentials.
The independent retained-result reader checks this trace when present. Older
checkpoints remain readable but provide no new capability proof.

These are implementation and evidence-production paths, not installed provider
qualification. A run with no continuation or missing negative tool controls
cannot prove those capabilities. Local process termination does not establish
remote cancellation. Lost Pod/node recovery, independent native-effect counts,
installed isolation and the complete original release criteria remain separate
qualification requirements. Offline continuation tests use synthetic local
children and never execute the vendor CLI or submit native work.

## Verification and remaining integration

After executor bootstrap and worker installation, invoke the deployed Transform
worker with `{"l5_runtime_probe":1,"connectivity":"transform"}` to verify its own
EKS transport and scoped read permissions. The ordinary
`{"l5_runtime_probe":1}` remains a factory-only check. The optional probe makes
at most three GET requests, each with a two-second transport timeout and a 64 KiB
response bound: the protected node pin, then the named `deny-all` NetworkPolicy
in each of the two protected namespaces. It checks the exact node UID, readiness,
absence of deletion, and complete deny-all policy. It needs no namespace-list or
cluster-admin permission and creates no Pods or provider jobs.

Retain the Lambda invocation metadata with this result to establish which
deployed identity and artifact were checked. Fixed diagnostic codes distinguish
network timeouts, EKS authentication/RBAC refusal, replaced or retiring nodes,
and missing or changed policies without returning endpoint, namespace, node or
session values. A passing response always states `qualified: false` and
`live_acceptance: not_evaluated`: ingress/egress enforcement, native acceptance
and promotion still require their separate evidence.

The normal-worker test is
`modules/L5-services/tests/transform/test_runtime_transform.py`. With the shared
runtime module installed, it constructs the real factory and drives durable
source capture, pending baseline, one provider launch, uncertain-response
reconciliation, candidate checks and retained publication. Only Gateway HTTP,
Kubernetes, S3, database connection creation and queue publication are replaced.
The tests do not execute the vendor binary or establish live qualification.

Gateway registration loads the three service-owned exports from
`src.l5_backend_transform.gateway_backends()`. The shared `bind_backend` hook
provides each request's authenticated actor, database session and admission
idempotency identity. Admission issues the existing Product-scoped work grant
and uses the shared source-snapshot capture path. No Task identity is fabricated,
and constructing the exports performs no AWS or filesystem I/O.

Protected Gateway configuration supplies `AWS_REGION`,
`L5_TRANSFORM_ARTIFACT_BUCKET`, and `L5_TRANSFORM_QUALIFICATION_REF`. The latter is
a JSON object with `bucket`, `key`, `version`, and `sha256` strings identifying an
immutable private S3 qualification document. Its contents are a list of records,
each containing `scope` (tenant, Product and project IDs), `executor_binding_ref`,
`provider_identity`, `expires_at`, `transform_qualification` (the existing
`ProviderQualification` schema) and `transform_test_policy` (the pinned policy
including its exact `test_policy_ref`). The release owner supplies independently
qualified records; an offline fixture must never be installed as qualification.
The reader checks the object version, digest, exact selection, scope, expiry and
provider pins. Missing, stale or unsupported qualification denies admission.
Store the qualification document at the exact key
`l5/transform/qualification/catalog.json` in the bootstrap's artifact bucket.
This is outside the expiring `l5/transform/artifacts/` prefix. The coordinator
pins the published version and SHA-256 in `L5_TRANSFORM_QUALIFICATION_REF`;
never select an unversioned latest object or install synthetic node evidence.

The bootstrap installs the selected Gateway role's read-only
`s3:GetObjectVersion` policy on exactly these resources, substituting the
selected private bucket name for `<artifact-bucket>`:

- `arn:aws:s3:::<artifact-bucket>/l5/transform/qualification/catalog.json`
- `arn:aws:s3:::<artifact-bucket>/l5/transform/artifacts/*`

The Gateway needs neither `s3:ListBucket`, latest-object `s3:GetObject`, writes,
checkpoint discovery, Kubernetes access nor provider-launch authority. These
Gateway reads are separate from the coordinator-owned worker artifact/checkpoint
ceiling; no common ceiling changes are made here. A non-null `executor` without
`gateway_role_name` fails planning. Repository-only bootstrap with
`executor = null` still creates no IAM policy and needs no Gateway role input.

Integration tests compose the shared Gateway and retained-source contract at
`2afe1b8b067bfdba0d606b477f41ebd620bb335f`; that dependency must be present in the
assembled release. The normal-request regression is
`modules/gateway/tests/products/test_transform_backend.py`.
`modules/gateway/tests/products/test_transform_retained_source.py` exercises
the actual mounted source reader after work completion, including revocation
and corrupted original bytes.

The control implementation archives terminal observations, the original patch
reference, check receipts and cleanup identities in versioned storage before
removing its own Pod finalizer and deleting UID-verified scratch inputs. A lost
delete acknowledgement resumes from that retained checkpoint; a repeated collect
returns the same original patch version. Native failures are cleaned only after
both a definitive provider outcome and Pod termination have been observed.
Missing or contradictory native observations leave a paid attempt uncertain and
retain its resources; they never authorize a replacement paid launch. Uncertain
jobs may therefore consume the bounded quota until independently reconciled.
Release qualification must use the existing collector
and evaluator with independent Product/provider observations and original patch
readback; passing offline fixtures are not release acceptance.
New retained artifact IDs are URL-safe opaque encodings so the Product artifact
download route can address them without interpreting storage-key slashes as path
separators. Storage reads continue accepting previously retained key references.

## Read-only release qualification

The release entry points consume existing completed runs; they do not submit a
transformation, resume work, obtain provider credentials or delete resources:

```bash
modules/L5-services/scripts/qualify-transform-release.py preflight SOURCE_SHA FIXTURE_PATH EVIDENCE_DIR
modules/L5-services/scripts/qualify-transform-release.py run SOURCE_SHA FIXTURE_PATH EVIDENCE_DIR
modules/L5-services/scripts/emit-transform-qualification.py SOURCE_SHA FIXTURE_PATH EVIDENCE_DIR
```

`SOURCE_SHA` is the reviewed 40-character release revision. `FIXTURE_PATH` is a
private JSON file (mode `0600`); `EVIDENCE_DIR` is a private directory (mode
`0700`) outside the checkout. Replace those placeholders with authorized private
inputs. The fixture selects exact identities and revisions, not evidence or
callbacks. Connection settings may be supplied in its optional `runtime` map
(with the exact environment names listed below), or through the protected
process environment. Fixture values take precedence; no arbitrary environment
names, AWS credential fields or executable callbacks are accepted:

```json
{
  "source_sha": "0000000000000000000000000000000000000000",
  "runs": [{
    "tenant_id": "example-tenant",
    "product_id": "example-product",
    "project_id": "example-project",
    "run_id": "example-run",
    "result_id": "example-result",
    "result_revision": 1
  }]
}
```

The L5 wheel includes the reviewed shared `runtime_support` module and its RDS
trust bundle; loading the worker or qualification commands requires no temporary
source overlay. IAM database sessions use these packaged roots with hostname
and certificate verification enabled.

Prerequisites are the installed L5 package with shared runtime resources, Git
(for independently applying the original patch), the retained Product source
routes, and private connectivity to Gateway, RDS and the Transform artifact
bucket. Protected configuration supplies `L5_GATEWAY_URL`,
`L5_QUALIFICATION_API_TOKEN` (a current Product-authorized bearer token),
`AWS_REGION`, `L5_TRANSFORM_ARTIFACT_BUCKET` and the shared `L5_RDS_HOST`,
`L5_RDS_PORT`, `L5_RDS_DATABASE`, `L5_RDS_USER` inputs. The qualification identity
needs read-only database access to runs and original attempts and read-only
artifact/checkpoint access; it needs no Kubernetes or provider access. Its
checkpoint discovery permission is separate from the narrower Gateway policy.
Install the retained-source API dependency before invoking these commands.

The driver reads durable admission and original attempt records, then re-reads
the retained snapshot under current Product authorization. It checks exact
source bytes and modes, native identity, terminal executor checkpoint, original
patch bytes, and independently retained baseline/candidate check receipts and
logs. An expired worker lease is not reused: qualification has a separate,
short-lived read authority, with fresh Product checks before artifact reads.
Missing checkpoints, revoked sources, changed revisions or fabricated receipts
fail instead of accepting fixture assertions. Execution's active-work source
API remains unchanged.

Preflight validates private configuration offline, before the release workflow
assumes its AWS role; it does not claim credential or readback validation. `run` writes
`observations.json`; the emitter repeats the independent reads before writing
`qualification.json` and does not trust that earlier file. Existing outputs are
not overwritten. Reports contain sanitized digests and result revisions, with
`live_accepted: false`; they evaluate the selected original results, not
installation, two-project portability, owned cleanup or complete native live
acceptance. Those remain the release coordinator's separately observed gates.

Offline reproduction uses the repository's pinned Python development tools:

```bash
cd modules/L5-services
python -m pytest -q tests/transform/test_qualification_release.py tests/transform/test_qualification_driver.py tests/transform/test_qualification_evaluator.py
```

The tests retain the actual source verifier, SQL run/attempt reads, Product result
router, artifact reader and evaluator while mocking external HTTP and S3
boundaries. They are implementation evidence, not installed/native qualification.
