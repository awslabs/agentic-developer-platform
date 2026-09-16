# Active primary-team claim reconciliation (#5032)

Changing a primary team in the selected organization must update the Cognito
attributes used by the next authentication or refresh. Team membership, the
`users.team_id` cache, and Cognito attributes are separate stores: changing the
Postgres pointer alone does not change the token.

The replace, add, and remove membership endpoints now finish with
`commit_team_memberships`. This commits the membership transaction, resolves the
existing verified canonical-login/org-local linkage, acquires the canonical
login row lock used by workspace selection, and reloads the org-local account
and authoritative primary membership. Selection and synchronization use the
same primary-resolution function; the legacy pointer is a fallback only when
there are no membership rows.

The Cognito adapter resolves the immutable subject to its canonical username,
then reads current attributes with `AdminGetUser`. `ListUsers` attributes are
not used for the current-org check because that API is eventually consistent.
Only `custom:team_id` and `custom:department_id` change, and only when the edited
org is selected. Removing the last team clears both attributes. Editing another
org does not select it; role, identity, and unrelated attributes are preserved.
Users without a login need no Cognito update. A broken or ambiguous explicit
link is an error; email is never used to infer a login.

The database and Cognito are not atomic. If Cognito fails after the membership
commit, the endpoint returns HTTP 503 with
`detail.code=team_membership_claims_sync_failed` and `membership_saved=true`.
The caller should retry the same request before refreshing its session. Every
retry reconciles the current committed primary, including idempotent add and
remove operations; an old request cannot replay an outdated primary snapshot.
A database failure before the first commit does not write Cognito. There is no
background retry or promise of repair without a subsequent successful request
or workspace selection. Already issued JWTs retain their old scope until
refresh or expiry, and a refresh during a failed synchronization may still read
old attributes.

Both reconciliation and workspace selection serialize claim writes through the
canonical row lock. Failed workspace selection previously restored saved claims
after releasing that lock; that could overwrite a later selection or team edit.
Compensation now reacquires the lock and derives claims from current committed
membership and team state. A unique active org wins. For legacy multiple-active
rows, only the previously verified Cognito org can be used, and it must still
have valid active membership. A later successful switch leaves a unique active
org, so even an A → B → A sequence cannot restore an obsolete team snapshot.
If authoritative membership cannot be resolved, compensation fails explicitly
instead of inventing a replacement workspace.

Validation uses real admin HTTP routes, a stateful Cognito double, the actual
pre-token function, and the actual gateway context conversion, budget/rate
hierarchy, and Bedrock routing resolver. PostgreSQL 16 tests exercise both lock
orderings, delayed older reconciliation, a failed selection followed by a later
selection, and the same-org A → B → A case followed by a primary-team edit. These
are local tests; they do not substitute for #4907's live ordinary-user browser
and refresh evidence.
