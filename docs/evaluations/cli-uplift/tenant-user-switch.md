# Tenant default isolation across users — E27 extension

The existing `tenant-isolation` suite can additionally check CLI-09-AC-04's user-default separation without inference, account mutations, or another schedule. The installed CLI performs all tenant reads and default writes; legitimate session acquisition and swapping in the disposable EC2 client's private stores are fixture setup, not login-flow acceptance.

Add this optional object to the existing `tenant_isolation` fixture:

```json
{
  "tenant_ids": ["first-authorized-tenant", "second-authorized-tenant"],
  "user_switch": {
    "ordinary_fixture_name": "existing-owned-fixture-secret-name",
    "ordinary_login_user_id": "exact-ordinary-login-subject",
    "ordinary_tenant_id": "ordinary-native-tenant"
  }
}
```

The original installed session must see both explicit tenants. The ordinary fixture must have its known native membership, a different login subject, and a native tenant different from the original session's saved default. Existing scoped secret-read permissions must already allow the fixture name; this extension grants no access.

After existing concurrent tenant/default/refresh checks, E27 records the original saved default, switches to the ordinary fixture, verifies that a single membership selects natively or multiple memberships require explicit selection, confirms the ordinary identity and native selection without inheriting the original default, saves an ordinary default and checks a tenant-scoped capability read. It restores the original config/token files even on a partial write or failed command, switches back and verifies the original identity/default. Only successful complete execution records `user_switch` evidence. The enclosing session handoff retains the original login.

No tokens are included in evidence. Existing fixtures without `user_switch` keep their behavior. The extension does not establish local inference isolation, membership revocation, lost mutation acknowledgement, deployment alias behavior, or full #5622 acceptance. Live EC2 execution remains required.
