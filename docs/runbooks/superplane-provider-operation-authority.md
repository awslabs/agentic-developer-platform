# Provider-operation authority and reconciliation

Record, conclude, recovery and release-assessment requests use a canonical
workspace UUID. The receiver checks the authenticated submitter's UUID grant,
resolves the Workspace in storage and binds the owning organization. Operations
have workspace and organization foreign keys; display names grant no authority.
Two tenants naming a workspace `dev` retain separate operation namespaces.

Writes additionally require `operation_authority`. A trusted validator must
resolve live B authority for the exact pre-call handle and authenticated executor,
including active operation/run/attempt IDs and a timezone-aware expiry. Those IDs
are persisted at creation and checked on every conclusion, including repeats.
Authority tokens are never stored or logged. Expiry is checked after resolution
and again before returning a permission-bearing result, including after commit.

B's live facade is not present in this repository. The startup-only integration
port `app.services.provider_authority.ProviderAuthorityValidator` has no validator
installed by default: writes return503. A future adapter must verify B's current
permission, lease/fence and full operation context. Test adapters demonstrate the
boundary only; A neither issues authority nor owns a replacement lifecycle.
Cross-attempt recovery requires B's explicit contract rather than implicit rebinding.

A delayed success supersedes an absence/retry conclusion. A newly learned
provider reference makes a claimed refusal ambiguous until identifying absence
evidence exists. Repeating the same confirmed absence and reference is idempotent:
`applied=false` and the original conclusion timestamp is preserved.

Conflicting references return409 but first persist each additional reference and
its outcome/provider evidence in `provider_reference_conflicts`. The original ID
is retained and the operation becomes unresolved, including after a previously
concluded success. Recovery lists expose every conflicting reference even if the
reporter loses the409 response. Ordinary conclusion reports cannot erase these
anomalies; clearing them needs an explicit B multi-resource recovery contract.

Release assessment requires a separate trusted `AllocationInventoryReader` and
an allocation-scoped `operation_authority`. The reader verifies active B cleanup
authority for the authenticated executor and exact workspace/org/allocation, plus
B's attestation of the canonical SHA-256 digest of the submitted provider report.
An ordinary workspace credential cannot authorize ABSENT claims. The adapter must
verify through B rather than echo a request digest or infer authority from a grant.
It must supply current, complete membership under B's allocation recovery fence,
including all independently billed compute, storage and network identities.
Neither observation keys nor operation names define membership. No reader is
installed by default, so exposure remains unresolved without the real integration.
The receiver binds the inventory to workspace, organization and allocation, checks
expiry/completeness and persists immutable resource identities independently in
`provider_allocation_resources`. Membership only grows: a later snapshot cannot
erase a known volume or network resource. Provider evidence must identify each
stored resource. Known operation and conflicting references omitted from the inventory prevent
release and zero exposure. Every conflicting resource must be independently
proved absent under the complete inventory before release can be reported.
Operations with no provider reference and unresolved resource exposure must have a persisted B mapping to an
inventory member queried by that operation's resource name or idempotency key.
A timed-out pre-call handle cannot disappear merely because unrelated resources
are absent. Mapping keys, like resource membership, are retained monotonically.
Duplicate operation names are safe only when this independent inventory and the
identifying evidence account for every operation and reference.

A durable `provider_allocations` row serializes evidence writes across every
operation and inventory snapshot for a workspace/allocation. Record, conclusion
and release acquire that row before consulting B. Conclusion reloads its operation
after waiting; inventory reads refresh cached membership. This is a database
serialization anchor, not an A-owned lifecycle or a replacement for B authority.

Unapplied migration013 adds four tables and their foreign keys. U23 owns separately
authorized deployment. Real PostgreSQL tests apply013 in random disposable schemas
and exercise lock waits, expiring authority, overlapping inventory and stale ORM
state. Run them with a disposable `SUPERPLANE_TEST_POSTGRES_URL` and
`python -m pytest tests/test_provider_handles_postgres.py`; without that URL they
are explicitly skipped. The ordinary CI lane still has no PostgreSQL service.
Test success does not establish live B integration, provider cleanup or usable
installed provisioning; those remain integration acceptance requirements.

Recovery lists retain successful/provider-present conclusions because those calls
still own outstanding resources. A fresh recovery client can retrieve the durable
provider reference (or original resource name/idempotency key when none was
returned) after losing the conclusion response. Confirmed absence/refusal may
leave the recovery list; appearing in it never grants permission to retry.

An operation durably concluded as a provider refusal/confirmed absence without a
reference needs no inventory member: no resource was created. Such a failed attempt
does not strand a later successful attempt after that actual resource is proved
absent. Recorded, ambiguous and provider-present attempts still need coverage.
