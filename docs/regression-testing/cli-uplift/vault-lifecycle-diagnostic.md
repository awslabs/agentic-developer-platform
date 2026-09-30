# Owned vault lifecycle diagnostic

`vault_lifecycle` is an opt-in dispatcher purpose using the installed EC2
fixture's ordinary selected-tenant human session. It performs no inference,
provider login, identity verification, role mutation or shared-secret change.
Select `login,vault-lifecycle` with `fixtures_json` to run D02 through normal
reporting and EC2 cleanup. D02 is excluded from default nightly/full suites.
E24's existing read-only coverage remains separate from this diagnostic.

It creates one synthetic user-scope `api_key` entry, repeats its exact operation
UUID, changes only metadata, verifies a stale-revision HTTP 409 refusal, refuses an
unsupported secret-rotation argument, and confirms deletion through metadata
absence. The synthetic value is passed only through stdin and never emitted.
AWS Secrets Manager's recovery window still applies; this is not proof of
physical secret erasure.

It also records one synthetic Discord identity claim, requires
`verification_method=self_asserted` and `verified_at=null`, checks pending
`link --resume`, and unlinks the owned claim. It never starts verification or
contacts Discord. If the claim gained external verification, cleanup refuses
to unlink it and reports pending recovery. Unrelated visible metadata must be
unchanged after the diagnostic.

The caller must persist the deterministic plan BEFORE SSM execution using the
externally backed manifest gate introduced by the chat durability correction:

```python
from tests.e2e.cli_uplift.remote.vault_lifecycle_plan import recovery_plan
payload["vault_lifecycle"] = {
    "owned_mutations_authorized": True,
    "login_user_id": "INSTALLED_FIXTURE_LOGIN_USER_ID",
    "canonical_user_id": "SELECTED_TENANT_FIXTURE_USER_ID",
    "tenant_id": "aws-e",
}
payload["recovery_plan"] = recovery_plan(payload)
# `manifest` must have its critical external persistence hook configured.
worker(instance_id, "vault_lifecycle", payload, manifest=manifest)
```

The normal journey payload must also supply stable `evaluation_id`, gateway,
`work_dir`, CLI path, instance binding, native `test_user_id` and on-instance
session reference. The served CLI selects the authorized tenant with
`ADP_TENANT`; server model-mapping readback must match the expected human and
tenant before any vault mutation. Do not use the operator login as the fixture.

The plan declares the entry UUID and exact provider/claim tuple without a value
or token. The worker requires the exact plan before touching the
fixture. The caller-side manifest therefore retains cleanup targets even if
the instance dies before returning stdout. Final `detail` retains metadata
revisions, actual identity ID if observed, checks and cleanup phases. A lost
creation acknowledgement is reconciled by the same entry UUID. Missing metadata
without a confirmed create/delete remains an unknown outcome, not proof that
no secret was created. A failed unlink does not prevent cleanup of the owned
entry; any remaining ambiguity fails the diagnostic.

Do not rerun a previous evaluation with unresolved owned targets. Read its
retained manifest/recovery evidence and reconcile those exact targets first.
