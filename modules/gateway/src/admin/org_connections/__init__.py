"""Platform-admin lifecycle for an organization's GitHub connection.

Issue #4842 (EPIC #4839 · C3), rulings R1 + R6 of
``docs/design-notes/4828-platform-native-org-team-user.md``.

An organization is created without any GitHub connection (that is the point of
the admin-created, GitHub-free tenant), so "connect one later" and "disconnect
it" need to be operations rather than side-effects of an install callback. This
module is those two operations.

**No new table (R1a).** The lifecycle runs over the EXISTING ``organizations``
GitHub columns and ``channel_tenant_map``. The design note is explicit that those
columns are load-bearing for the #4070 uniqueness guard, the #2724 provenance
gate, and the identity-index write-through — "GitHub is a connection" is a
presentation and lifecycle claim, not a licence to restructure the org table.

**Why this is not ``admin/connections``.** That module is the *self-serve* path:
a user installs the App on their own GitHub org and the callback binds it to
whichever tenant that user belongs to. This module is the *operator* path — a
platform admin binds a named installation to a named org, including orgs the
admin has no membership in. Different actor, different authorization, so a
separate surface rather than a flag on the existing one.
"""
