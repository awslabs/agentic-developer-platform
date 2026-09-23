# Learnings — issue #5676 (A22: require verified database TLS, reconcile local-session protection)

2026-09-21 AWS security scan, work package A22; parent #5677. Scope: `database.py`,
`schema_boundary.py`, the migration and seed connection paths, and focused TLS tests. Shared
`app/config.py` and `installation/manifests.py` belong to #5682 and were left alone.

Outcome: certificate and hostname verification is now mandatory and fail-closed on every path
that opens a Superplane database connection; transport security was separated from schema
selection; the finding's stated premise turned out to be false and the fix changed shape
because of it; a second latent defect was found in the same function; 41 new tests.

## What generalises

**"No TLS configured" and "no TLS" are different claims, and the issue said the wrong one.**
The finding read as "Superplane connects to its database without TLS", inferred from the
absence of any SSL argument. The assignment anticipated this and said so explicitly — *do not
claim TLS is absent merely because CA input is optional; test actual driver negotiation and
defaults* — which was the single most useful instruction in the ticket. Reading asyncpg 0.31.0's
`_parse_connect_dsn_and_args` showed the no-argument default resolves to `sslmode=prefer`:
encryption **is** attempted, the certificate is accepted unconditionally (`CERT_NONE`,
`check_hostname=False`), and on failure the driver **silently retries in plaintext**. So the
real defect was not missing encryption but *unauthenticated encryption with a non-deterministic
wire format and nothing logged about which one happened*. That reframing changes the fix: had I
"turned TLS on" by setting `sslmode=require`, I would have removed the plaintext fallback and
closed the finding while leaving the actually dangerous half — an attacker able to answer on the
database's address still passes, because encrypting to an unverified peer establishes nothing
about who you encrypted to. Verify the driver's defaults empirically; parameter names are not
behaviour.

**Assert on the library's resolved state, not on the dict you handed it.** My first attempt
passed `{"ssl": context, "sslmode": "verify-full"}`, which reads correctly and is wrong:
`asyncpg.connect` has no `sslmode` keyword and raises `TypeError` at the first connection — i.e.
in the deployed environment, not in any test that only inspected my own return value. A test
asserting `args["sslmode"] == "verify-full"` would have passed forever. The tests now call
asyncpg's own parser and assert on *its* resolved parameters, because the gap between those two
representations was the bug. The same run also killed `ssl="verify-full"` as a string, which
fails looking for `~/.postgresql/root.crt`. Two plausible-looking spellings, both broken, both
only detectable by executing the library.

**A regression test for a security fix should pin the old behaviour, not just the new.** The
suite contains a test that asserts what the *previous* default resolved to — `prefer`,
`CERT_NONE`, `check_hostname=False`. It documents the vulnerability in executable form and it
fails if a future refactor reintroduces the implicit default, which an assertion about the new
context alone would not catch.

**Two decisions in one function is a latent defect even when both values are currently right.**
`connect_args()` computed the search path and the transport posture together and returned one
merged dict. Nothing was wrong with the values, but the shape meant a future change to schema
handling could move the encryption posture as a side effect, and neither reviewer of that change
would be looking at TLS. Splitting it into `schema_connect_args()` and
`transport_connect_args()` — with a test asserting a schema setting cannot influence transport —
cost nothing and removed a whole class of future accident. The public `connect_args()` was kept
so none of the three call sites needed editing.

**Enumerate the artefacts in the test; do not hand-list them.** The manifest test discovers
every container in `deploy/*.yaml` that has `DATABASE_URL` in its env, including
`initContainers`, and demands trust material on each. It immediately caught two containers in
`integration-test.yaml` I had missed by hand. A hardcoded list of four files would have passed
and left two connections unverified — and would go on passing when someone adds a fifth. For
"every X must have Y" properties, the discovery step *is* the test; asserting Y on a list you
wrote yourself only tests your list.

**Different clients need different mechanisms for the same secret.** Three artefacts consume the
identical secret key (`ca-pem`) three different ways, because the seed Job runs `psql`: libpq
cannot take a CA from an environment variable at all, so it needs a mounted file plus
`PGSSLROOTCERT`, and `PGSSLMODE` must be `verify-full` specifically — `require` encrypts while
verifying neither chain nor hostname, which is exactly the posture being removed. One secret key
for all three keeps rotation single-step; the test asserts the *mechanism* per client rather
than a uniform env var.

**Fail-closed makes rollout order load-bearing, and no manifest can say so.** A pod with no CA
bundle now refuses to start. That is the point, but it means an environment that has never had a
`ca-pem` key takes a full control-plane outage for every tenant until the bundle is provisioned.
The code cannot express "do this first", so it went in a runbook, along with a deliberate
fail-closed verification step (remove the key, confirm the pod refuses) — because a verifying
client that silently degrades is indistinguishable from a working one until the day it matters.

**Match the emergency override against an exact literal.** The local-only exception fires only
on exactly `true`. `"false"`, `"0"`, `"no"`, `"True"` all leave verification on, and present
trust material wins over the override so a stale flag cannot keep an environment unverified
after it has been given a CA. Truthy-string parsing here would mean a careless value silently
disabling the fix. Parametrised over all of those.

**`setdefault`, not assignment, when a conftest sets an environment variable a test must clear.**
The shared conftest enables the local exception so the existing suite can use SQLite. Written as
`os.environ[...] = "true"` it would re-enable the exception under every test, making the
fail-closed tests unable to observe the refusal they exist to prove. The autouse fixture that
clears both variables per test is the other half of that.

**Separate pre-existing failures from your own before reporting, and prove it by stashing.** The
API suite shows 4 failures and the domain lane 2. All 6 reproduce on `main` with the branch
stashed (`git stash -u`), all are route-inventory drift or subprocess calls to a system `python3`
lacking `yaml`, and none touch database transport. Also worth recording: one earlier run showed
257 errors that were entirely `ModuleNotFoundError: cryptography` from an incomplete local venv.
Reporting either set as findings would have been noise; not checking would have risked reporting
someone else's breakage as mine, or hiding mine inside theirs.

**Pin the linter to CI's version before calling a lint result a finding.** Local ruff 0.16.8
reported 192 findings in this module; CI pins 0.9.6, under which it is clean. The count was
identical before and after my change, which is the check that matters — but the honest statement
is "clean at the pinned version", not "192 problems".

**Consume a sibling story's delivered work as evidence instead of rebuilding it.** The
local-session half of this ticket (A23, developer proxy origin guard) was already fully on
`main` from #5686. The instruction not to start competing implementations made the right move
running its 62 tests and citing them, rather than writing a second guard that would have had to
be reconciled later.

## Deployment / boundary notes

Nothing here was deployed. No secret was created, no infrastructure applied, no credential
rotated. Operator end-to-end verification (`pg_stat_ssl` showing the runtime session encrypted)
requires a deployed environment and remains outstanding live acceptance. The NetworkPolicy for
the Superplane service is #5682's file and its enforcement depends on still-open #4999 — and
rendering a NetworkPolicy would not have been evidence that isolation is enforced.
