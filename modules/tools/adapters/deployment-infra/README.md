# Adapter foundation deployment root

This Terraform root composes the existing [publication](../infra/) and
[access foundation](../foundation-infra/) modules. The AWS provider permits only
the explicit account and region. Both modules depend on verification of the
reviewed, active EKS cluster. Its described endpoint and CA configure Kubernetes;
`aws eks get-token` selects that exact cluster and region using the launcher's
credentials. No kubeconfig file is required.

All target inputs are explicit: `aws_account_id`, `aws_region`, `environment`,
`state_bucket`, `cluster_name`, `namespace`, `oidc_provider_arn`,
`runtime_role_name`, `runtime_verifier_role_arn`, and `runtime_verifier_group`. The existing child modules
validate their values and enforce the exact EKS OIDC issuer and STS audience.
`source_kms_key_arn` is optional for an SSE-KMS archive bucket. The archive bucket
must already be private and versioned; runtime publication verifies its archive
version and checksum. Supplying a bucket name does not establish those properties.

The root owns one immutable ECR repository, its image qualification project
and build role/log group, a separate foundation check project and role, a dedicated namespace, the adapter pod's IRSA trust
role, and separate namespace Role/RoleBindings for the read-only verifier and
protected deployment group. The build role
cannot publish to ECR. The pod role has no provider permissions. The workload
renderer retains ownership of its ServiceAccount, ConfigMap, Service, Deployment
and PodDisruptionBudget.

The foundation check project is `adp-tools-<environment>-adapters-foundation-check`.
Its Docker runtime executes the already deployed gateway and adapter image digests
with generated PostgreSQL and provider-transport fixtures. It reads only those two
image repositories and its own reviewed source prefix, and writes only its own
evidence prefix and logs. Explicit policy ceilings deny registry writes, deployment,
role passing, provider credentials, production database access and other projects'
archives. It has no VPC attachment. The existing image builder retains its original
permissions and cannot pull private images.

Both projects use `codebuild/src/<project>/` and `codebuild/artifacts/<project>/`
in the selected versioned bucket. The protected deployment role can dispatch the
foundation check and retrieve exact source/result object versions; it cannot write
the result or download the adapter image. The installer plans the new policy and
attachment alongside the existing publication policies, applies the foundation
first, and reads back both attached policies. Project and prefix outputs are
verified before bootstrap reports success. The protected workflow and independent
reader still have to verify actual runtime, test results and cleanup; provisioning
the check project does not establish acceptance.

The existing registered environment connection role receives only
`ecr:DescribeRepositories` and `ecr:BatchGetImage` on the exact environment's
adapter repository, plus `eks:DescribeCluster` on the reviewed cluster. These
reads let the controller resolve a pinned image after an interrupted publication
and independently observe runtime through its namespace RBAC. The root verifies
the role's full ARN and reads its existing `STANDARD` EKS access entry, requiring
the selected verifier group before provisioning either foundation. It does not
create or change that entry, the role's trust, or its other policies. This grant
adds no image publication, build, object archive, secret or provider access.

The deployment binding uses `adp:tools-release-<environment>`, the existing group
defined by `platform/automation-infra/tools-deployment.tf`. Its role permits
server-side apply of only the five named renderer resources, the named deployment
watch used for rollout, and the canonical observer's namespace reads. Creating
a missing resource by server-side apply requires both `patch` and `create`;
the API server checks the resource name on that create authorization. Ordinary
unnamed `POST` creation is not granted. Secret, token, exec, deletion, direct pod
mutation, RBAC changes and cluster-wide permissions are absent.

The normal installer must supply an environment-specific S3 backend key and
locking configuration, preserve the reviewed target inputs on updates, and
review/apply a saved plan. Do not apply either child independently after this
root owns its resources. Existing resources managed by another state require
an explicit state migration; this root does not silently adopt them. The
launcher should clear ambient `KUBE_CONFIG_PATH` and `KUBE_CONFIG_PATHS` so that
unrelated provider configuration cannot inject a kubeconfig.

The protected workflow's mutation IAM and EKS access entry, network policy, authenticated
release evidence, workload apply and provider access are separate integration
work. Creating this foundation alone does not establish an operational adapter
or a successful deployment.

The mocked tests require no AWS or Kubernetes access:

```bash
terraform init -backend=false
terraform validate
terraform test
```

The tests cover composition and refusal of a mismatched target. The existing
child suites additionally verify build permissions, IRSA trust and verifier RBAC.
