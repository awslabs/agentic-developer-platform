# Superplane authorization policy

Install this domain-owned package with `pip install modules/domain-apps/superplane/auth`.
Import `superplane_auth.policy`. It uses only the standard library and does not
change core ADP authentication or register gateway routes. `adp-common` remains
separately packaged general-purpose utilities.

`authorize_request` returns `(principal, grant, sanitized_headers)`; ingress must
forward only the returned headers. Signature/expiry verification happens before
this policy, using trusted server wiring, never a body-supplied validation path.
All grant and operation objects must come from server-owned storage; constructing
a dataclass is not proof of provenance. U14 must bind the retrieved operation to
the current invocation, validate its expiry/revocation and enforce resource ownership.

The endpoint inventory records required permissions and scopes. The composer
supports workspace routes, including cost/events with a server-resolved workspace
selector. It refuses organization collections (workspace create/list, accounts,
providers): a workspace grant is not organization authority. U14 must implement
an explicit org grant/operation path and filter collection results by workspace
authority before those endpoints can become reachable. No implicit org-admin
or org-mate permission is granted by this library.

U14 also owns live endpoint enforcement. Offline policy checks do not establish
that an upstream route rejects a request today, or that deployed credential-binding
flags enforce anything; target environment evidence must be supplied separately.
