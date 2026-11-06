# Security progress without browser automation

`modules/L5-services/tests/live/security_headless.py` checks saved, disabled
Security configurations through the Product API, CLI and MCP. It independently
compares their configuration identity, revision, scope, settings and readiness
gaps. Each of two dedicated tenant sessions must successfully read its own
configuration and receive a nondisclosing denial for the other tenant through
all three surfaces. Generic transport failures do not count as denied access.

This path has no browser dependency and starts no provider jobs. It accepts a
private `security_headless_progress` fixture with two distinct tenant, Product
and Project scopes, dedicated sessions and CLI homes, exact configuration
revisions and an expiring authorization for `configuration_readback` and
`tenant_isolation`. It uses the repository CLI and refuses redirected HTTP
requests. Keep fixture files and evidence outside the checkout with mode 0600
and directories with mode 0700. No personal operator session belongs in these
fixture homes or CI secrets.

Run from the reviewed checkout:

```sh
python modules/L5-services/tests/live/security_headless.py \
  REVIEWED_SHA PRIVATE_FIXTURE PRIVATE_OUTPUT
```

The output is private partial progress, with `release_qualified: false`. It is
not a selector receipt and cannot replace any of the existing full release
acceptance selectors. Served UI setup, native assessment results, worker
recovery/cleanup and Context reuse remain explicitly unverified.

The existing full acceptance fixture has additional real dependencies:

- `assessment_live.py` requires two pinned assessment source plans, an
  authorized source-event endpoint and receipt, and independent review files
  bound to the actual completed run and native job.
- `recovery_live.py` requires exclusive native/worker scopes and an authorized
  HTTPS fault controller for uncertain submission and publication
  acknowledgements. Creating two Products does not provide that controller.
- `context_live.py` requires a completed Security publication, downstream
  Context and agent query/readback endpoints, independent audit/visibility
  observations and separate credentials for those authorities.

Missing endpoints, provider results or independent reviews are unfinished
implementation or live prerequisites. Do not populate them with fabricated
URLs or receipts, or restart full qualification until they are usable.

`modules/L5-services/tests/live/l5_configuration_headless.py` reuses these
read-only checks for Security, DevOps and FinOps. Its separate fixture kind is
`l5_configuration_headless_progress`, with an explicit `service_id`; it checks
one service across both tenant cases under one deadline. All selected
configurations must already exist and remain disabled. It allows only CLI
`config show` and MCP `get_configuration`, and retains separate UI, provider,
recovery and downstream acceptance as pending.

DevOps and FinOps can save source-free disabled drafts after their capabilities
are explicitly selected on the Product. This does not authorize provider
connections or establish native readiness. Transform requires an authorized
repository and connection, and Well-Architected requires pinned authorized
sources even for disabled drafts. The source-free progress runner excludes
those services; missing fixtures must not be replaced with invented sources
or changes to service admission.

### Installed publication evidence readback

Security qualification reads the retained publication through
`GET /v1/products/{product_id}/projects/{project_id}/services/security/runs/{run_id}/publications/{publication_id}`.
It uses the current Product access session; no separate custom publication URL
or publication credential is required. The endpoint checks current owner,
source and delegation access, the exact result/native/source lineage, payload
hash, durable result receipt and invalidation watermark, then repeats the
current authorization and publication checks before releasing the copy.

This readback does not establish Context acceptance by itself. Qualification
still compares the consumer's independently stored receipt and projection,
the later authorized agent's exact citations, and denied-scope observations.
An installed endpoint or passing offline test does not complete SA16.

The Security worker now mounts the installed per-run publication client when an
exact qualified `L5_CONSUMER_CONTRACTS` entry is present. Without that contract,
Context delivery remains disabled and the existing native worker can still load.
The repair scan selects Security-owned contributions and resolves each original
run's delegation; no global tenant or operator session grants publication access.
