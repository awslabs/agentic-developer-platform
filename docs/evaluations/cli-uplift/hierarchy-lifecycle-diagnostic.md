# Owned hierarchy and ordinary tenant revocation diagnostic

The explicit `hierarchy-lifecycle` suite runs D03 on the existing installed EC2
harness. It is excluded from `full` and `nightly`; no inference, customer AWS
resource, login deletion, or role-policy change occurs.

Supply `fixtures_json` with an existing independent administrator and ordinary
member. The ordinary native membership and secondary target membership must
already exist. Its target membership must be active with role `member`, empty
`team_id` and no teams; any other baseline refuses before mutation.

```json
{"hierarchy_lifecycle":{
  "owned_mutations_authorized":true,
  "login_user_id":"ADMIN_NATIVE_USER_ID",
  "canonical_user_id":"ADMIN_TARGET_TENANT_USER_ID",
  "tenant_id":"aws-e",
  "ordinary_login_user_id":"ORDINARY_NATIVE_USER_ID",
  "ordinary_canonical_user_id":"ORDINARY_TARGET_TENANT_USER_ID",
  "ordinary_native_tenant":"adp-platform"
}}
```

The existing fixture secret must contain `non_admin_username` and
`non_admin_password`; these are read in memory using the instance's existing
secret grant. The initial ordinary authentication request is fixture setup.
Lifecycle operations use the installed CLI in separate temporary stores. Both
canonical identities are verified before writes, and ordinary administrator
login must be refused. No token or password enters manifest/evidence.

Before SSM dispatch, the existing durable manifest critically records exact
owned org/department/two-team IDs and the expected restoration baseline. Failed
persistence or changed inputs refuses dispatch. Preexisting target IDs refuse a
new diagnostic; recover the prior intent instead of changing IDs.

D03 creates and renames an empty disposable org, observes a stale-revision CLI
refusal, creates one department and two teams under the existing target tenant,
refuses populated-department deletion, adds the ordinary fixture to both teams,
and proves removing one retains the other. A wrong-parent lookup cannot expose
the team. Both team assignments are removed before membership revocation.
The org stays empty: adding a user would retain an identity/tombstone after
membership removal, legitimately preventing org deletion.

It then revokes only the ordinary target membership, verifies CLI target access
and new selection are refused, and verifies native-tenant reads still succeed.
A separately retained pre-revocation signed lease is checked through the API to
corroborate old-lease denial; that API check is explicitly separate from the CLI
fresh-selection checks. The native login and other tenant are preserved.

Finally it explicitly reactivates the same ordinary membership, verifies its
original empty-team baseline, removes only owned team edges, deletes the exact
owned resources child-first, and requires absence readback. Failure cleanup
still runs; ambiguous cleanup or restoration remains failed/pending. Unexpected
resource names or changed ordinary role/team state are preserved for explicit
recovery. No original user or unrelated membership is deleted.

The diagnostic's manifest intent survives an instance lost before any output.
The generic resource sweep does not automatically interpret it: use the retained
fixture IDs and restore baseline to recover manually, then exact owned IDs to
clean resources. Do not dispatch another evaluation ID to conceal pending work.
Source fault tests cover lost creation reply, wrong actor/foreign baseline,
revocation not enforced, failed restoration/cleanup, and pre-dispatch persistence.

Organization creation also creates its canonical default department and team. The v2 recovery plan records their exact IDs before create transport. Cleanup removes them child-first only when parent, default name/description, and team department still match; changed children remain pending. This includes accepted creation with a lost reply.

The acceptance extension retains explicit department/organization and
team/department parentage. Cleanup also fences those parents and preserves a
resource whose parent changed. It attempts an ordinary user's hierarchy read and
update through the served CLI, requires refusals and unchanged administrator
readback, and verifies that `--name Default` cannot substitute for a canonical
team ID. The CLI supports canonical selectors only; this is a name-selector
refusal, not implemented name resolution. A write may be stopped by the CLI's
permission preflight or its protected snapshot read; the diagnostic does not
claim a denied PATCH reached the server when it did not.

One team create is retried with the exact same caller ID and body: the current
server must return a conflict and the original snapshot must remain unchanged.
Its deletion is retried with the original ID/revision after cleanup and must
report absence. Neither outcome is relabeled a successful idempotent mutation.
The original failed evaluations remain failed; this extended case requires a
new independently reported execution before it supplies live evidence.

Member role-change authority remains a concrete hold. Placing the existing
ordinary person in the disposable organization creates an org-local identity;
removing its membership intentionally retains that row and prevents clean
organization deletion. The served CLI has no identity-deletion command, so D03
keeps its disposable organization empty. It never elevates the shared tenant's
ordinary member or changes AWS IAM. Role-transition qualification needs an
explicit owned membership fixture with a supported retention/cleanup contract.
