# Moving existing build infrastructure into Superplane ownership

This is a preparation procedure, not an executable migration or authorization.
The existing platform CodeBuild module still owns `superplane-api`,
`superplane-controller`, `superplane-monitor` and `superplane-executor` projects,
their project roles and inline policies. This change does not remove them:
removing their definitions now would plan destruction. The new unmerged paid
project is isolated first and needs no state migration unless an operator has
independently deployed the earlier platform manifest.

Before migrating existing resources, implement app-owned definitions matching
their actual configuration and stable names, plus a reviewed platform exclusion
input that preserves today's ownership by default. Do not reuse the paid-worker
root for unrelated buildspecs or widen its role. Keep the shared CodeBuild
boundary, source bucket/lifecycle and unrelated projects in platform state.
Existing shared boundaries must remain attached until an equivalent app-owned
boundary is reviewed and available. Do not delete a shared boundary during the
transfer. Inventory log groups and their actual owner before importing them.

For each confirmed account, region and environment:

1. Inventory the exact project, role, inline policy, boundary attachment and log
   group identities; record source revision and both backend identities/state
   lineage/serials. Establish whether a resource is Terraform-owned or unmanaged.
   Prepare exact old-to-new Terraform address mappings from this inventory.
2. Quiesce project configuration changes and automatic/manual build dispatch.
   Verify no queued or running builds depend on the transfer. Coordinate an
   exclusive maintenance window for both state owners; a lock on only one
   backend does not serialize two independent deployment pipelines.
3. Back up both states privately, including backend versions. State can contain
   secrets; do not attach it to issues. Prepare a reviewed transfer/import and
   forget-without-destroy procedure for the installed Terraform version. A
   cross-state transfer is not atomic. Specify recovery for a failure after
   either state write, retaining the exclusive maintenance window until both
   owners agree. Never run an apply while both or neither state owns a resource.
4. Move ownership without changing AWS resource names, deleting resources or
   recreating projects/roles. Update platform exclusion and app definitions
   together under the coordinated deployment revision. Do not run plain
   `terraform state rm` followed by an ordinary platform apply: the old
   definitions would attempt to recreate what was removed from state.
5. Refresh and inspect saved plans from both roots. Require no project/role
   destruction or replacement, no unrelated platform changes and no permission
   widening. Any intended boundary attachment update is reviewed separately.
   Verify project ARN, role ID, source/buildspec, environment, timeout, logs and
   effective policies directly before resuming dispatch.
6. Record the final ownership inventory and update all deployment/teardown paths
   so future platform applies preserve the exclusion. Teardown must act only on
   app-owned resources and must never remove the shared source bucket/boundary.

Rollback restores coordinated ownership/configuration from the reviewed state
backups while dispatch and both deployment pipelines remain paused. Do not
restore an old state after unrelated applies or attempt recovery by deleting live
resources. Recheck lineage/serial and actual AWS identities before recovery.

Exact state commands and backend files cannot be supplied until the target,
installed Terraform version, resource inventory and maintenance scope are
confirmed. No migration, import, state removal, apply or cloud mutation has been
performed by this preparation.
