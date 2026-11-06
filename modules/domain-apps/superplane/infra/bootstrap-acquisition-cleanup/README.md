# Native acquisition cleanup authority

This disposable module gives the installation owner the minimum AWS access to
remove the three original bootstrap access entries and temporary Kubernetes
administrator access to revoke the original journal's Kubernetes grants. The
original actor roles, trust policies, namespace and workspace infrastructure are
retained. All four resources in this module must be destroyed before publishing
owner recovery evidence.

The normal `adp-<environment>-superplane-installer-<installation>` role remains
supported. If the protected enrollment instead names a local installation
operator, supply that existing role's exact name and immutable IAM `RoleId` as
`installer_role_name` and `installer_role_id`. Run the saved plan and apply as
that operator. The module verifies the live IAM ARN and RoleId and the current
STS account, role name and RoleId. A name alone does not authorize another role.
The owner must first verify those values against the protected enrollment and
installation receipt; this module does not replace that owner verification.

For EKS deletion, use the maintained `scoped_entry_client` with the owner session
as its source and the disposable cleanup role as its target. That role's inline
policy permits deletion only for the three original immutable entry ARNs, and
each native call further restricts its STS session to one entry ARN. The enrolled
operator need not trust itself for role assumption. Do not substitute an
unrestricted owner client: an entry replacement at the same principal alias must
remain unauthorized by the deletion session.
Pass `session_name="sp-acquisition-cleanup-" + generation[:24]` to match the
cleanup role's exact trust condition, and bound the source session's
`_superplane_authority_deadline` to the cleanup role's reviewed expiry.

Derive `entry_arns` from the original journal's three immutable EKS access-entry
identities. Set `expires_at` no more than one hour after the plan time, review
the saved plan and apply it once. Use the unchanged native
`qualify_cleanup` / `recover_acquisition` / `collect_revoked` path to validate
expiry and revoke the original generation, then destroy this module and verify
absence of its role, inline policy, access entry and policy association.

Local tests use mocked AWS providers and do not contact AWS:

```bash
terraform init -backend=false
terraform test
```
