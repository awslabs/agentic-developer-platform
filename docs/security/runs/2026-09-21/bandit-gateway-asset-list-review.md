# Gateway asset list/count boundary review

Original observations: `bandit|bandit-results.sarif|run=0|ri=1118` and `ri=1119`,
B608, MEDIUM, frozen source `b1d0894c17c686f27c2747057dead0b5a0e6b17e`,
`modules/gateway/src/knowledge/routes.py` lines 353 and 364. The inventory retains
both exact identities, original locations, severity and #6108 ownership.

The count and page queries interpolate `where_clause`, but every condition in
that clause is a source-controlled literal. Tenant, canonical owner, asset type,
status, limit and offset are passed separately as bound values. A SQL-looking
asset-type payload is executed against SQLite as data and returns no rows; it
never enters the SQL string. This review does not suppress B608 or claim that
the scanner stopped reporting the expressions.

Review exposed an authorization error beside those expressions: the default
list filtered by tenant/shared scope without checking personal ownership, while
the detail endpoint checked canonical ownership for non-admin callers. The fix
applies the same personal restriction before both counting and pagination.
Tenant-wide and shared unowned assets remain visible, explicit personal/tenant
filters retain their behavior, canonical IDs are used, and current admin
behavior remains intact. This bounded change does not resolve any broader
question about administrator authority or ownership of legacy unbound rows.

`modules/gateway/tests/knowledge/test_asset_list_visibility.py` executes the
production route and its generated SQL against a disposable SQLite database
with mixed tenants, canonical/external identities, personal/shared rows and
removed assets. Six tests cover default isolation, both explicit scopes,
existing admin behavior, pagination/count isolation and SQL parameter binding.
Against the unchanged source, default isolation and pagination/count tests fail
(2 failed / 4 passed); with the fix all six pass. The entire gateway knowledge suite
passes 201 tests. Existing gateway lint/format checks pass for the modified code.
These are local application/SQL checks, not PostgreSQL or live gateway acceptance.

No gateway/tick deployment or live workload operation was performed. Those
holds remain. The other original observations are unchanged: 1470 total,
4 prior fixed logging handlers, 187 source-verified test assertions, 2 reviewed
parameterized SQL observations, 1277 pending source review under #6108.
