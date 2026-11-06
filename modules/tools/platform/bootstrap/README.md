# Tools bootstrap and release state

The platform state owner, not the push-triggered tools release role, owns this
root. It defines the IAM roles and policies, encryption keys, repository,
execution table, façade build project and its log group. The release root in
`../infra` defines only the AgentCore gateway, targets and resource policy. Its
role and key inputs must come from approved bootstrap outputs in protected
configuration; the checked-in examples are not release credentials.

Do not apply either root against an old combined state. Before enabling the
release workflow, the state owner must approve and rehearse a migration against
private state snapshots at a reviewed revision. The existing state and live
resources have not been verified by this source change.

1. Disable all tools apply/dispatch paths for a maintenance window. Confirm the
   actual account, selected roles, remote state location, serial/lineage and
   live resource inventory under approved authority. If a foundation resource
   does not exist, create it through the reviewed bootstrap process, not the
   routine release role. Determine the gateway ARN before planning the façade
   IAM policy. Do not apply the old combined root with the narrowed release role.
2. Back up the combined state in the private operator store. Establish separate
   bootstrap state at `tools/bootstrap/dev.tfstate` and keep the release state at
   `tools/platform/dev.tfstate`. Using reviewed Terraform `state mv -state`
   and `-state-out` operations **on private local copies**, move *every* IAM,
   KMS, ECR, DynamoDB, CodeBuild and CloudWatch resource instance to bootstrap
   at its unchanged address. Keep only AgentCore gateway, targets and resource
   policy instances in release. Preserve instance indexes, lineage and backups;
   never use `state rm` to hide an unmanaged resource or let two roots manage it.
3. With exclusive state authority still held, review each resulting `state list`
   against its root's resource inventory, then publish the two states through
   the approved locked state procedure. Review **both** root plans with exact
   private input references. A plan that creates, destroys or updates an
   unapproved foundation resource is a stop, not an invitation to grant IAM.
   The release script rejects any non-AgentCore or destructive change.
4. Supply bootstrap outputs for `gateway_role_arn`, `gateway_kms_key_arn` and
   `assertion_signing_key_arn` through the protected tools configuration. Match
   the selected gateway ARN in bootstrap's `gateway_arn` input. Under platform
   state owner authority apply the dedicated `tools-release-rbac.tf` Role and
   RoleBinding, verify the access entry's exact group, and pre-create only the
   named Deployment, Service, two ServiceAccounts and suspended reconciler
   CronJob through the approved bootstrap process. The release Role has no
   `create` permission; missing objects must stop release, not expand access.
   The registered gateway and target ARN inputs must come from approved private
   outputs, not an invented wildcard or source-controlled environment identity.
5. An opt-in managed interceptor grants the gateway role invoke permission
   only on a future `:live` alias, without creating that alias or attaching it
   to a gateway. After real code publication and hash/alias readback, the state
   owner reviews a separate bootstrap plan supplying the same managed alias and
   bootstrap-owned interceptor execution role as inputs so the selected shared
   target Lambda receives key verification permission. Do not grant it through
   the release role. Confirm the deployed interceptor is a verified numbered
   real version before enabling protected targets. Only after the platform owner signs off both
   state plans, RBAC and protected environment may the release workflow use the
   release state. Preserve snapshots and prior receipts for rollback; do not
   destroy keys, state or other workloads to recover.

## Managed interceptor capacity

The optional `interceptor_reserved_concurrent_executions` owner input defaults
to `-1`, retaining the shared account pool. The dev overlay at
`environments/dev/modules/tools-bootstrap.tfvars.json` sets it to `10`, matching
the selected shared-tools Lambda. Merge this overlay with the reviewed private
bootstrap inputs, then inspect a scoped plan for the existing interceptor
function before applying. This input changes neither the published code nor the
live alias. Read back the actual function concurrency after apply before using
the configured capacity in a native test quote. The overlay is not a complete
bootstrap configuration and does not replace account, identity or state inputs.

## Outbound OAuth permissions

Vendor targets require a separate bootstrap IAM update; the routine AgentCore
release role must not attach IAM policies. Before first enabling GitHub, Jira
and Slack targets, the platform state owner supplies `gateway_oauth` through
protected bootstrap configuration:

- `workload_identity_arn`: the release root's `gateway_workload_identity_arn`
  output, available on the existing gateway even with OAuth targets disabled.
  This is the gateway-generated workload, not the ADP `-oauth-user` workload.
- `providers`: entries keyed by `github`, `atlassian` and `slack`, each containing
  `provider_arn` and `secret_id`. Use the exact reviewed provider ARN under
  `token-vault/default/oauth2credentialprovider/adp-tools-<environment>-<vendor>-user`
  in the target account and region, and the same external secret ID used by the
  release root's `oauth_provider_secret_ids`. The provider names are fixed by
  the release configuration, so these grants can precede provider creation.

Review and apply that bootstrap plan before releasing the enabled targets.
After release, its `gateway_oauth` output provides the complete reference object
for comparison and subsequent bootstrap updates. Keep it in protected
configuration, not public evidence. Changing or replacing the gateway workload,
providers, secrets or secret encryption keys requires another reviewed bootstrap
update before the affected targets are used.

Bootstrap resolves only secret metadata (`DescribeSecret`) and, when present,
encryption-key metadata (`DescribeKey`); it never reads secret values into state.
The gateway role receives `GetWorkloadAccessToken` for its exact workload and
directory, `GetResourceOauth2Token` for the three exact providers, and
`GetSecretValue` for their external client secrets. Customer-key `kms:Decrypt`
is restricted to those secrets through Secrets Manager; the key policy must also
permit IAM-authorized use. `gateway_oauth = null` grants none of these permissions
and performs no secret lookup. Mock-provider tests cover this policy contract,
not live OAuth credential retrieval.
## Private gateway network role

The account-wide AgentCore Gateway network service-linked role is an optional
foundation resource, owned by `enable_gateway_network_service_linked_role=true`.
It is independent of target enablement and retained with `prevent_destroy`.
Identify an existing owner before importing an already-created role; never own
the shared role from two environment states. The release role keeps its IAM
mutation denial. Follow [Knowledge first enablement](../../../../docs/tools/knowledge-first-enable.md)
for the separate network grants and protected saved-plan binding.
