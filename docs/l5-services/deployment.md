# L5 Security worker release and recovery

This is the **code path**, not evidence of a deployed or approved Security capability.
Follow the [platform deployment authority](../adp-platform-deployment/deploy-with-agent.md)
before selecting a private target, role, time/cost allowance or fixtures. No
infrastructure is provisioned by the offline checks. The gateway's Security
admission authority currently refuses new runs; no worker factory is verified
as loadable in the installed archive. Do not enable the queue consumer or repair
rule until those dependencies and the live prerequisites are verified.

1. On a reviewed main ancestor, explicitly run **L5 Services Release**
   in a protected environment. It publishes one versioned contract wheel,
   worker ZIP and manifest to that environment's private release bucket. Record
   the source SHA, wheel SHA-256 and **manifest object version** privately.
2. Deploy additive schema through the existing gateway migration mechanism. Use
   the [independent L5 root and release installation stage](modular-deployment-design.md)
   for Terraform. Do not enable the old gateway-owned L5 toggle for new installs.
   Initial worker mappings and repair rules remain disabled. Existing resources
   require the documented ownership transfer before the independent root applies.
   Supply the private target configuration and pinned archive coordinates; verify
   the installed gateway wheel before selecting a worker release.
3. The protected **L5 Security Worker Lifecycle** workflow offers `status`,
   `rollout`, `rollback`, `resume` and `teardown` (pause and retain). Rollout and
   rollback take a reviewed source SHA, independently observed gateway wheel
   digest and the manifest version. They verify the versioned archive bytes,
   stop both consumers, update the queue and repair functions using revision
   fences, then read back both code digests. They **leave both consumers paused**.
   A failed or partial update also stays paused; retry with a compatible
   previously pinned manifest using `rollback` rather than inventing a new
   provider job or deleting work. Compatibility is restricted to the same
   gateway wheel digest; a differing digest needs separate review.
4. Run `resume` only with the same verified manifest, after checking the
   configured worker factory loads from the installed archive, scoped Product
   and broker permissions, disabled-admission behavior and authorized recovery
   fixtures. The offline factory-presence check is **not** that live proof.
   `resume` refuses mismatched code and tries to pause again if enabling either
   consumer fails. Synthetic recovery and live acceptance remain separate.
5. `teardown` stops both consumers and reports retained SQL history, queues and
   provider assets. It **does not delete** these resources. The queue Terraform
   definitions block destruction until a reviewed drain, authoritative cleanup
   readback and deliberate removal of those guards. Never infer cleanup from a
   workflow success; use the private receipt and native storage readback.
   Each lifecycle action reads the actual Lambda execution roles, inline grants
   and boundary attachments, plus both queues' identities, retention and
   approximate backlog before changing consumers. Mismatched policies or missing
   readback fail closed with a private failure receipt. The operations manifest
   maps L5 changes to an **unresolved** protected workflow: no private target,
   release pin or gateway runtime verifier has been approved. A private receipt
   is not proof of effective denied calls, SQL history or provider cleanup.

The workflows serialize artifact publication and lifecycle changes for each
protected environment. They verify the selected account and exact assumed role;
no ambient credentials substitute for that selection. Lifecycle receipts,
including failed-operation and retained-resource status, go to versioned private
artifact storage under the workflow run identity. Check the private receipt and
reconcile a lost workflow response before retrying; do not publish identities or
fixture evidence in the repository. Ordinary worker Terraform updates ignore
worker code and consumer-enable drift; the explicit lifecycle owns those fields.

## Installed-release probe contract (SCN-01)

Keep one **private** versioned JSON fixture manifest. Its `schema_version` is `1`
and its `installed_release` section supplies the exact pinned source revision,
gateway image, installed package version, wheel/schema/CLI/frontend hashes,
worker archive's Lambda `CodeSha256`, release-manifest object version, selected
account and role, queue, single database head, Product, unique probe and
nonce, expected result hash,
run-owned cleanup identifiers, `max_attempts` (1–3), owner-reviewed
`max_spend_usd` (a positive decimal amount) and `deadline_seconds` (1–59).
The short deadline tests queue dispatch without waiting for minute repair.
Never commit the populated manifest or observation bundle.

`l5_services.installed_release_probe` loads these pins and checks separate
STS, versioned artifact, Kubernetes, authenticated gateway schema, Lambda, CLI,
frontend, SQS, metered spend, stored result, Profile and native cleanup
observations. Supply distinct private `evidence_ref` values for each source
and each Lambda attempt. Record two separate Profile read references and
native absence, or explicit retained-resource reasons. Every observation must
be timestamped and no more than fifteen minutes old; Lambda queue mapping has
its own native readback.
The reader that collects each observation must verify the selected
target and capture its own provider response: passing this pure offline verifier
with synthetic observations is **not** live acceptance. The three live selectors
remain pending an approved target, scoped identity and bounded fixtures.

With `uv` and Python 3.12 available, run this offline from the repository root:

```bash
uv run --project modules/L5-services --extra dev pytest -q modules/L5-services/tests/deployment/test_installed_probe.py
```

The test uses synthetic observations and provisions no infrastructure.

## Recovery probe contract (SCN-02)

Keep a private `schema_version: 1` manifest with a `recovery` section. Pin the
selected account and role, owned queue, source revision, shared wheel and schema
digests, two versioned worker/repair archives and their Lambda code digests;
identify one Product, run, work item, source snapshots and cleanup resources.
Bound the test by `deadline_seconds` (60–900), `max_attempts` (1–3) and an
owner-reviewed positive `max_spend_usd`. Never commit the populated fixture.

The pure `l5_services.recovery_probe.verify_recovery` checks independently
supplied native readbacks for rollback, automatic repair, one provider effect,
no early polling, retained history and snapshots, bounded attempts/spend/time,
rejected new admissions and cleanup failures recorded with a retention reason.
It does not collect native observations or inject a live queue fault.

## Effective-scope probe contract (SCN-03)

A private `schema_version: 1` manifest with an `effective_scope` section pins
one selected account/role, gateway role, work queue, two distinct Product and
tenant grants, a run-owned probe ID, exact allowed/foreign operations and
resources on the API, source, artifact, provider and queue surfaces, and the
principal for each call. Pin the reviewed worker, repair and gateway policy
hashes and both IAM boundary hashes. List every run-owned resource to remove
or retain, and bound `deadline_seconds` (1–900), `max_attempts` (1–3) and
`max_spend_usd` (positive decimal). Keep the populated manifest and all native
responses in private storage, never public documentation.

`l5_services.scope_probe.verify_effective_scope` compares fresh independent
STS, Product grants, IAM policy/boundary versions, allowed/denied calls,
separate native provider and database absence checks, budget attempts and
native cleanup observations. It refuses re-pinned broad queue policies,
permissive boundaries, wrong-Product disclosure, reused/stale readbacks and
unexplained retained resources. The IAM check is not a substitute for real
denied calls; both are required. The probe only verifies supplied evidence.
Do not enable it against a target without selected credentials, owner-approved
fixture limits and cleanup authority. With Python 3.12 and the package's test
dependencies installed, the offline contract can be checked with:

```bash
modules/L5-services/.venv/bin/python -m pytest -q modules/L5-services/tests/deployment/test_scope_probe.py
```

This offline test uses fictional observations and makes no AWS calls.
