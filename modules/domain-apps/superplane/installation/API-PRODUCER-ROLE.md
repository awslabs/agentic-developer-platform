# Optional managed API producer role

Implementation design approved for source work; this document grants no live
deployment or AWS mutation authorization.

The existing control-plane role trusts both API and controller service accounts
and cannot satisfy the dedicated producer verifier. The optional
`api_producer_role` environment field selects a new module-owned
`adp-<environment>-superplane-api-producer` role. Its target API ID and stage are
explicit; account, region, namespace and management OIDC identity come from the
validated environment and platform state. When adapters are selected, their
dispatcher must name this exact role and target. Omission preserves existing
behavior. Existing control-plane and SkyPilot resources are unchanged.

The role trusts only `superplane-api` in the selected namespace, using exact
OIDC provider, `StringEquals` subject and `sts.amazonaws.com` audience. Its one
inline policy grants only `execute-api:Invoke` on the six exact controller-execution POST
routes plus `/internal/v1/credential-evidence`. Evidence uses the same regional
API Gateway endpoint and renewable workload identity. Only exact original three-
or six-route policies can upgrade to seven; the role ID and trust stay pinned.

Every installer Terraform invocation forwards the desired optional role value,
including resume and later upgrades. The role shares the maintained domain module,
state and exclusive lock. No separate transition engine is introduced.

Preflight may defer only an exact `NoSuchEntity` result for the explicitly managed
role. A colliding or incorrect existing role always fails the strict verifier.
Before a plan is accepted, inspect the saved plan's exact role name/ARN, trust,
actions, resources and creation semantics against the selected intent and
management OIDC identity. Bind that evidence to the reviewed plan hash. Unknown
or substituted authority values fail closed. A CREATE may leave the derived
ARN and AWS-assigned RoleId unknown until apply; trust, policy, name and path
must already be exact. The Terraform role owns its one inline policy and an
explicitly empty attached-policy set so its planned authority is inspectable.

AWS provider 6.67 may report `name_prefix` and `managed_policy_arns` as computed
on CREATE. For a preflight-verified absent role with no prior Terraform identity,
the saved-plan guard accepts a computed prefix only alongside the exact known
name and no configured prefix. It accepts computed attachments only when that
same saved plan's configuration contains a literal empty list and its planned
value is absent or empty. Omitted or referenced configuration is insufficient.
Trust, inline policy, path and name remain fully known and exact, and separate
attachment resources are still refused. This exception never applies to updates
or existing identities. Live verification also requires the managed role's
attached-policy list to be empty, even if an attached policy duplicates the
permitted routes; externally managed producer roles keep their existing verifier.

For an explicitly selected target, preserve this field on every later plan:

```yaml
api_producer_role:
  api_id: abcdefghij # Replace with the existing reviewed producer API ID.
  stage: dev
```

Selecting the canonical managed role ARN without this field is refused before
Terraform. Omitting it from a later plan that still has the role in state is
also refused by the installer's existing no-deletion gate. Removal requires a
separate reviewed cleanup; omission is never implicit authorization to delete.

After the approved saved plan is applied, inspect Terraform output ARN and RoleId,
then run the strict live role verifier and bind its identity to those outputs.
This check precedes foundations, API service-account changes and rollout. Adapter
transport snapshots continue to require the selected cluster, Gateway Service
identity and explicit IAM evidence mode/endpoint/region; missing-role handling does not waive any other prerequisite. Resume
rechecks live identity rather than trusting a completed phase marker.

Remote CI must cover default omission, exact Terraform trust/route plans,
incorrect existing roles, plan substitution, post-apply identity mismatch and
resume. Local validation is restricted to source/static checks.
