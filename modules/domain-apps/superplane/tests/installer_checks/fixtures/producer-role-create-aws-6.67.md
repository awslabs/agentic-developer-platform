# Producer role creation plan fixture

Reduced from a real Terraform 1.14.0 saved control-plane plan using AWS provider
6.67.0 and app source `db852f87065ab666cf0ea6a5f1ca902581562244`.
The original private plan SHA-256 was `eb07b85bdca97605759e2f8d9f3b5ed263b7dfd3c0f6477401f72f7acdacc324`.

Only the producer role resource change, matching configuration resource and plan
versions are retained. Account, API ID and OIDC issuer ID were replaced with test
identifiers. Field presence, nulls, computed-value markers, configuration
expressions, seven inline-policy routes and sensitivity markers are unchanged.
No full installation plan or credentials are committed.

The provider reports `managed_policy_arns` and `name_prefix` as unknown on create,
even with a literal empty managed-policy list and an exact known role name.
The tests must use this provider-generated shape rather than assuming an empty
list remains known in the planned result.
