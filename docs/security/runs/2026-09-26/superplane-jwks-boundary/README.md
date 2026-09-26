# Superplane JWKS authority fixture — #6108

Exact pending Bandit B310 selector `run=0|ri=758` identified an untested key-fetch
boundary. `_JWKSCache._fetch` used urllib's default redirect handling; a configured
JWKS endpoint could redirect to another location whose keys would become trusted
for signature verification. This requires control of a redirect from the configured
endpoint; it is not a claim that an arbitrary token holder can control Cognito.
The free-string configuration also selected arbitrary urllib scheme handlers.

The URL is now parsed before dispatch: only HTTP(S), a hostname and a valid port;
credentials, fragments, ASCII controls (C0/DEL) and whitespace and backslashes are refused. This
preserves configured HTTP development endpoints and HTTPS Cognito endpoints while
excluding file/ftp/data handlers. A per-request opener refuses redirects without
changing global urllib behavior. The five-second timeout, direct endpoint, real
RSA verification, caching and refusal error contract remain.

Thirty new fixtures cover all five redirect statuses at same and different
origins, direct key reuse, a different signer, malformed documents, outage recovery,
and fifteen forbidden/malformed URL cases with zero provider calls. Twenty-five
fail against the baseline, including ten redirect cases where the redirected keys
validate the presented signature. All 144 auth tests pass with the fix. Bandit
reports one B310 before and none after; behavioral fixtures, not scanner absence,
establish the source fix. Runtime acceptance remains open.

The isolated local run uses `run-isolated-cli-test.py` and the repository's API,
auth, contracts, workspace bootstrap, lifecycle and executor packages on Python's
module path, matching the packages installed by Superplane CI. All HTTP fixtures
bind only to ephemeral loopback ports and bypass ambient proxy settings explicitly.
No live credentials, external JWKS endpoint, database or deployment was accessed.

The receipt preserves original MEDIUM severity separately from source applicability.
All 1,470 selectors remain; 911 still require source review. Serving-image rebuild,
deployment and authentication against the actual configured JWKS remain owned by
#6108 and the Superplane release owner. Existing configured HTTP transport risk
and key-cache lifetime are separate; this patch makes no HTTPS-only deployment claim.
