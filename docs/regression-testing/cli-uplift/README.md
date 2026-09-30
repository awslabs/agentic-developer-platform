# CLI-uplift evaluation — IAM artifacts

Least-privilege policy documents applied to the evaluation's own roles, kept here
so a live grant is reviewable in the repo rather than existing only as console
state. These roles are not Terraform-managed (they were created for the
evaluation under issue #5248 and are tagged `Purpose=cli-uplift-eval`).

| File | Role | Policy name | Why |
|------|------|-------------|-----|
| `orchestrator-policy.json` | `adp-cli-uplift-eval-orchestrator` | `cli-uplift-eval-orchestrator` | Existing base policy captured September 26, 2026; only launch dependency security-group ARN changed from the egress-disabled default group to the already configured no-ingress, HTTP/HTTPS-egress devbox group. |
| `orchestrator-e02-fixtures-policy.json` | `adp-cli-uplift-eval-orchestrator` | `cli-uplift-eval-e02-fixtures` | E02 provisions its own Cognito challenge + non-admin identities and a run-owned secret; the base policy is read-only for Cognito and could not create or delete them. |

## Scoping notes

- **Cognito is scoped to the single dev user pool ARN.** IAM cannot scope
  `AdminCreateUser`/`AdminDeleteUser` below the pool, so the pool ARN is the
  tightest available resource. Verified: `AdminCreateUser` on any other pool in
  the account is `implicitDeny`.
- **Secrets Manager is scoped to `adp/cli-uplift-eval/adp-e2e-*`** — the run-owned
  fixture prefix only. Verified: `CreateSecret` against the shared login fixture
  (`adp/dev/gateway/test-admin-credentials`) is `implicitDeny`, so a run cannot
  overwrite or delete the fixture the login-regression suite depends on.
- **`DeleteSecret` and `AdminDeleteUser` are included deliberately.** Without
  them the run can create fixtures it cannot remove, which is how an evaluation
  leaks real identities into a shared pool. Cleanup treats a denial as a failure
  rather than a clean teardown, so the absence of these was visible as
  `cleanup: failed` (run 35099197539) rather than silently ignored.

To re-apply:

```bash
aws iam put-role-policy \
  --role-name adp-cli-uplift-eval-orchestrator \
  --policy-name cli-uplift-eval-e02-fixtures \
  --policy-document file://docs/regression-testing/cli-uplift/orchestrator-e02-fixtures-policy.json
```


## Protected GitHub OIDC admission (#6004)

`orchestrator-trust-policy.json` is the reviewed trust document for
`adp-cli-uplift-eval-orchestrator`. It adds only the GitHub OIDC provider with
`aud=sts.amazonaws.com` and exact subject
`repo:aws-e/adp:environment:dev`. The existing shared-runner/scaledjob AWS
principals are retained for compatibility until their callers are audited;
this artifact does not grant any new service actions or change session duration.

Before updating the live trust, re-read the role and compare its current AWS
principal statements with the retained statements in this file. Stop if another
operator changed them; reconcile the reviewed artifact rather than overwriting
concurrent changes. Verify the `dev` GitHub environment admits only the `main`
branch (no tags), then configure `AWS_CLI_UPLIFT_EVAL_ROLE_ARN` in that environment.
Other environment names require separately reviewed trust and role bindings.

The workflow explicitly clears ambient credentials and uses OIDC without role
chaining. Both evaluation and recovery use the same role selection and reject
non-main refs. Source merge alone does not prove deployment: acceptance requires
live trust readback, successful OIDC exchange with the expected role, and the
existing bounded evaluation/cleanup checks. The executor workflow independently
uses the reviewed-project build dispatcher in `adp-build-dev`; its binding and
existing reviewed executor project must be provisioned before dispatch.


Rollout observation (2026-09-25): the supervising operator applied this additive
trust after a freshness comparison and AWS Access Analyzer validation, and
verified the exact OIDC condition plus unchanged existing AWS principals, service
policies and 10,800-second session limit. The `dev` and `adp-build-dev` GitHub
environments now admit only the `main` branch. At this checkpoint, the eval role
binding and actual OIDC exchange have not been verified, and the executor build
identity/project rollout is still pending. These remaining checks must pass
before claiming live workflow compatibility.

## Gateway deployment provenance when Lambda releases independently

The dev gateway and orchestration Lambda were observed on different images on
2026-09-25. Neither image had a commit-shaped tag, so the old Lambda-tag fallback
could not identify the actual gateway. The `gateway_deployment: dev` binding now
selects the explicit gateway observer. Configs without that field retain the
legacy health/Lambda behavior. Once configured, observer failure is terminal;
it never falls back to the Lambda or the caller's expected revision.

The observer exchanges the current orchestrator role's locally signed EKS token
and reads only `adp-gateway/Deployment/bedrockgateway` and
`adp-gateway/Service/bedrockgateway`. It requires the exact active cluster, matching
service/deployment selectors, positive desired replica count, observed generation,
all replicas updated/ready/available, no old/unavailable/terminating replicas, and
completed rollout conditions. It then resolves the gateway container's exact ECR
digest through `gateway-deployment-receipts.json` for legacy images. Modern releases
can instead resolve the exact digest through one full Git SHA tag in the reviewed
ECR repository. The observer verifies the repository account/name/URI, its
`IMMUTABLE` tag policy, and the returned image account/name/digest. Mutable
repositories, missing or ambiguous source tags, and identity mismatches stop the
run. The requested revision is still compared independently; it is never copied
into the observed result. Arbitrarily tagged legacy images still require reviewed
build receipts. The normal preflight
still compares the resolved revision with the requested revision and every served
CLI file with Git-derived hashes; the receipt does not replace those checks.

The reviewed capacity receipt binds gateway digest
`sha256:dda452b0e4d254e37f4753ac171e5fa81f0ad2fe0113237ea0256c1a8420bb4a`
to source `fe9396e67264029277a12499890a583c1d4a22e6`. Independent checks found:

- The saved ZIP and uploaded S3 source object match a fresh Git archive of that SHA
  byte for byte, with archive SHA256 recorded in the receipt.
- Successful CodeBuild metadata matches the unique source key, `ADP_SOURCE_SHA`,
  and image tag. Its CloudWatch push line records that exact tag and digest.
- The live gateway Deployment uses that digest. All 15 served CLI files enumerated
  by the source installer plus the installer itself match their committed hashes.

This is reviewed build provenance, not a claim that a digest inherently reveals
its Git revision. A reviewer must repeat that chain for every new receipt. The
catalog also binds the named cluster/service to the reviewed dev gateway URL;
it does not automatically discover or change ingress/CloudFront routing. Do not
reuse a binding after routing ownership changes without reviewing that mapping.
The observer sees the controller's settled Deployment/Service metadata, not
per-pod source inspection, and grants no pod-read/exec access.

### Stream-history release receipt (2026-09-25)

The gateway subsequently rolled to digest
`sha256:5e3a8b9e8b7900cd144e9924baf7a7fb63baa2534bbfda1249c5f09514c194a8`,
from source `d8b8a793774f9390c0f28da2499dc9d541b6e369` and successful build
`adp-dev-gateway-build:83489c91-afb4-4630-84bf-d2795e54fe84`.
The catalog retains the earlier receipt and adds this independently verified one:
the uploaded source object, saved build ZIP and fresh Git archive have identical
SHA256; CodeBuild source and `ADP_SOURCE_SHA`/image tag match; the build log records
the exact tag's pushed digest. The archive hash and source key are in the catalog.

Evaluation run `36100004960` failed closed during that rollout with
`Gateway deployment is unsettled or degraded`, before provisioning any resources.
Its durable manifest was empty, cleanup completed and the lease was released.
The deployment subsequently settled, but its new digest needs this receipt before
evaluation can accept it. A future run must still independently check rollout
readiness, use this release's full expected source revision, and verify served
CLI hashes; adding a receipt neither runs nor passes an evaluation.

### Global stream-quota release receipt (2026-09-25)

The current gateway image is
`sha256:fddd951f2375102355024db47f5e405ca331bfb991ac3f028ce6871f0a826260`,
built from `a16a2a851d17998b8cd51dfa47b77275251229cc` by successful CodeBuild
`adp-dev-gateway-build:f7125871-a75e-4289-ae87-cf37ba7612c9`.
Its uploaded source archive matches a fresh Git archive of that revision, and the
build's push log binds the `task-api-global-stream-quota` tag to this exact digest.
The catalog records its source key and archive hash and retains previous receipts.
All 15 served CLI files, including the installer, were independently compared
with the committed installer file list and Git bytes at this source revision.

This is provenance for the currently deployed image, not an evaluation acceptance
result. No receipt or acceptance claim is added for the interrupted S10 image
rollout. Future evaluation still requires a settled deployment, the exact expected
revision above and its own served-byte checks.

### Scoped observer rollout (supervisor only)

These artifacts are prepared, not applied by the PR:

- `gateway-observer-policy.json`: only `eks:DescribeCluster` on the exact dev cluster.
- `gateway-observer-access-entry.json`: the existing eval role mapped to a custom
  Kubernetes group, with no managed EKS access policy association.
- `gateway-observer-rbac.yaml`: namespace Role/RoleBinding with `get` on exactly
  the named Deployment and Service. No list/watch, pod, Secret, exec, or write grant.

Re-read current role policies, EKS entry and namespaced Role/RoleBinding before
applying; stop and reconcile any conflicting existing owner. Add the named
observer policy without replacing the orchestrator's existing policies. If the
access entry already exists, preserve its current groups and reconcile explicitly
rather than replacing it wholesale. These are additive observer permissions, not
new deployment authority. Review commands (execute only by the supervisor):

```bash
aws iam put-role-policy --role-name adp-cli-uplift-eval-orchestrator \
  --policy-name cli-uplift-gateway-observer \
  --policy-document file://docs/regression-testing/cli-uplift/gateway-observer-policy.json
aws eks create-access-entry --region us-east-1 \
  --cli-input-json file://docs/regression-testing/cli-uplift/gateway-observer-access-entry.json
kubectl apply -f docs/regression-testing/cli-uplift/gateway-observer-rbac.yaml
```

Read back exact policy, access entry/group and RoleBinding. Verify with the actual
OIDC role that the two named reads succeed while listing pods, reading Secrets,
reading another Deployment and writing the target are unauthorized (use read-only
authorization checks; never attempt a destructive mutation). EKS permission
propagation or an ongoing rollout must fail closed, not trigger a broader grant.

Before a bounded evaluation, recheck the gateway remains at the reviewed digest,
its rollout is settled, and its served CLI bytes still match. Then use the existing
workflow from main with environment `dev`, mode `start`, suites `login`, no fault,
empty evaluation ID and the verified full gateway revision. Preserve the exact
run handle and inspect both evaluation and recovery. `login` is E01+C01, not full
E02 or complete CLI acceptance. `mode=status` is not a read-only workflow probe:
restoration claims a durable lease, and cleanup/recovery still run.

### CLI stories release, 25 September 2026

The catalog includes gateway source `890b5d1a8b16d0fbf657dd258bf7535730592f04`,
published by canonical operator image maintenance. The successful CodeBuild
`fe111093-4da2-4157-9026-dea660396386` push log binds its full SHA tag to
`sha256:3f2b46db9b17b33921e31246dc95a8e15ecffdf556ae5ae89f2dbe85b2577aa4`.
The S3 source ZIP matches a fresh `git archive --format=zip` of that commit
byte for byte (`a90982364f324a298e2c5467942ee3e99fcdf3011b6af949fc93cc25f47e53b2`).
Migration completed before the gateway image change; all seven replicas became
available and the scheduled engine was synchronized and verified. All 22 public
CLI downloads match that source. Live evaluation run `36202049241` stopped at
the missing catalog receipt before creating EC2; cleanup reported no instances.
This receipt permits retrying the existing preflight, without weakening its
rollout, source revision, or public artifact comparisons. It does not claim
that the story scenarios have passed.

### EC2 launch security-group binding

Evaluation 36207990534 reached preflight and then `RunInstances` was refused for
`sg-0f497fc6d4ec88610`. The configured group has no ingress and only TCP 80/443
egress; the previous allowed default group has no egress and could not register
with SSM. `orchestrator-policy.json` preserves every existing base-policy
statement and replaces only that security-group ARN in
`LaunchDependenciesInApprovedSubnet`. This is a permanent evaluation fixture
binding; no worker role or task-dependent permission changes are involved.

```bash
aws iam put-role-policy --role-name adp-cli-uplift-eval-orchestrator \
  --policy-name cli-uplift-eval-orchestrator \
  --policy-document file://docs/regression-testing/cli-uplift/orchestrator-policy.json
```

For uncertain launches, the harness retains a stable EC2 client token and finds
pending cleanup intents by both exact evaluation and attempt tags. It requires
complete discovery, rejects mismatched tags and verifies termination before
claiming cleanup. An AWS refusal is not evidence that an instance was launched.
