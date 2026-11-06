# Identity helpers

## Tenant principal resolution

One person can hold several `users` rows (GitHub onboarding, Cognito first login and
admin invite each create one; see #8011). A token's `user_id` is the Cognito sub, which
indexes the person's *login* row — often their personal org — while the row that holds
their team, budgets and memberships in a tenant is the *tenant member row*
(`users.org_id == tenant`). Every owner or principal check must therefore resolve through
`src.agentauth.chat_authority.tenant_member_principal(db, tenant_id, subject, username=...)`:
it fuses the person's rows with `src.shared.identity.workspaces.person_user_ids` (the
`org_placement` link, the signed `GitHub_<id>` username when a token is present, and proven
GitHub/directory identities) and returns the single current member row for the tenant,
refusing by name (`chat_principal_not_tenant_member`, `chat_principal_ambiguous`,
`chat_principal_unknown`) instead of carrying an out-of-tenant id. Chat admission
(`external_roots.canonical_human`), the Crew owner (`crew.proxy.resolve_crew_owner`), the
Dev Box owner (`cloud_desktop.routes.owner`) and host enrollment
(`cloud_desktop.host_identity.current_host`) all use it, and
`tests/crew/test_owner_identity.py` enumerates them so a new owner check cannot pick a
different resolver (#8108). `workspace_user` remains for request routing that is not an
ownership decision.
