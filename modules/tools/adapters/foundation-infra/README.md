# Adapter foundation access

This child module provisions a dedicated namespace, a pod IRSA trust role and the
namespaced read permissions used by the canonical adapter runtime observer.
The renderer continues to own the ServiceAccount, ConfigMap, Deployment, Service
and PodDisruptionBudget. The role has no AWS permission policy at foundation time.

The parent deployment must supply all inputs and bind its Kubernetes provider to
the same reviewed EKS cluster. This module checks the AWS account, region, cluster
ARN, OIDC issuer and STS audience; a child module cannot inspect a caller's opaque
Kubernetes provider configuration. Use the cluster's described endpoint and CA
with credentials for that target, and verify the target before applying.

The parent also owns EKS access-entry mapping for the existing verifier group,
workflow mutation access, network policy, publication infrastructure, release
admission and independent post-deployment acceptance. Instantiating this module
is required in the protected foundation stage and the normal installation path;
these files alone do not enable deployment.

Pass `namespace` and `runtime_role_arn` outputs to the reviewed adapter renderer.
The IAM role trusts only the `tool-adapters` ServiceAccount in that namespace.
Provider token and key permissions require a later reviewed Terraform change.
Existing namespaces, connections, roles and GitHub App registration are separate.

The verifier can read the five named renderer objects, list and reread controller
objects in this dedicated namespace, and GET the exact adapter service proxy. It
has no secret reads, pod exec, token creation or write verbs. Kubernetes RBAC does
not constrain a list by labels; the observer additionally validates selectors,
ownership, source, image and release bindings.

Run offline provider-mocked checks with `terraform init -backend=false` followed
by `terraform test`. Real deployment still requires the parent target checks,
reviewed plan, effective IAM/RBAC/network verification and native receipts.

The optional `observation_runtime` input prepares the connection-observation
boundary. It reuses the existing core IAM API, adapter role, assertion key and
private ALB/VPC link. It adds a dedicated private API and execute-api endpoint
for core-to-adapter observation, with private DNS disabled so the existing core
API remains reachable. Both API and endpoint policies allow only the selected
core role; the adapter registry entry receives only the three adapter operations
for the configured tenant. No cross-tenant or worker privilege is granted.

Supply these bindings through the normal adapter bootstrap configuration. Keep
`release` null while preparing infrastructure. Preparation does not create an
OAuth grant or switch the four connector routes. The separate
`atlassian_mcp_identity_enabled` flag grants the existing isolated MCP workload's
two token-read actions when ready for observation qualification.

After verifying a matching observation configuration and immutable image, set
`release` to that exact release. Terraform publishes one atomic SSM manifest at
`/adp/<environment>/gateway/tool-adapter-config`. Both Gateway deployment paths
validate and render this manifest, including its JSON release, into the
ConfigMap. A missing optional manifest leaves observation disabled; malformed
configuration, access denial or a read failure stops deployment. Preserve the
runtime input on subsequent bootstrap runs; the saved-plan gate refuses its
removal as a destructive change.
