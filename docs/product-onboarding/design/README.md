# Product Profile and Onboarding — design for review

**Ready for design review, not approved for implementation or live acceptance.**
This proposal lets a tenant member save an Idea, adopt existing assets, or describe
a running product without provisioning anything. UI, installed CLI and agent
guidance use the same authorized operations and durable progress. Discovery fills
known information; people decide only unresolved choices, conflicts and approvals.

Design story: [#8153](https://github.com/aws-e/adp/issues/8153), including the
[owner amendment](https://github.com/aws-e/adp/issues/8153#issuecomment-6246940784).
Parent: [#8141](https://github.com/aws-e/adp/issues/8141); release umbrella:
[#5411](https://github.com/aws-e/adp/issues/5411). Issue bodies/comments were read
on 2026-10-18. Source baseline: **`56a8450aa41cc661c52ad90a1e421b8a16aca7f8`**.
This replaces neither the preserved PO/PON requirements nor the parent's pending
AI-DLC gate. It supplies the separately requested design review, not gate approval.

## Reading order

| Artifact | Purpose |
|---|---|
| [Source audit](source-audit.md) | Existing/proposed/unavailable distinction, pinned evidence, exhaustive inventory and coverage limits |
| [Shared data model](data-model.md) | Portfolio/Product/Project/Initiative/Contribution/Release relationships, source ownership and Engineering Insights compatibility |
| [Consistency review](consistency-review.md) | Cross-artifact behavior/ownership mapping, prototype checks and remaining service contracts |
| [Architecture](architecture.md) | Boundaries, reuse decisions, identity, authorization, discovery, events and dependencies |
| [Database](database.md) | ERD, physical names/types/constraints, transactions, migration and recovery |
| [API](api.md) | Operation semantics, errors, concurrency, callbacks and examples |
| [OpenAPI](openapi.json) / [schemas](schemas.json) / [examples](examples.json) | Proposed versioned, machine-checkable wire contracts; not deployed endpoints |
| [UI and CLI](experience.md) | Routes, wireframes, all three journeys, direct use, guided use and bidirectional resume |
| [Interactive HTML mockup](../mockups/product-onboarding.html) / [placement and usage](../mockups/product-onboarding.md) | Current ADP Blueprint shell, proposed Products entry, working local onboarding and simulated CLI |
| [Filed BOLTs and stories](../delivery/README.md) | Medium-sized implementation, deployment where needed, live verification and qualified acceptance mappings |
| [Delivery](delivery.md) | Component/pipeline map and original increment provenance; current sequencing is in the BOLT index |
| [Evaluation](evaluation.md) | Independent oracles, shared fixtures, negative cases, cleanup and AC/SCN coverage |
| [Requirements coverage](requirements-coverage.md) | All epic ECs, preserved PO/PON aliases and all 74 historical checklist obligations |

## Principal decisions

1. Extend the gateway's modular application and PostgreSQL database; no new
   service, queue, vault, invitation system or cloud datastore for v1.
2. Product is a shared business identity; governed Project is a delivery/access
   scope. They are not the personal Knowledge Layer's soft project filter.
   Creation saves Product + new/selected Project + dated binding atomically.
3. Reuse live tenant/team identity and credential services. A Product relationship
   can narrow access, never confer repository, cloud, spending or execution rights.
4. Persist typed configuration, provenance and onboarding progress independently
   of chat. Use optimistic revisions and durable operation records for retries.
5. Keep ongoing understanding in Context (#8149), consumer scope integration in
   #8148, provider operation in L5 (#8150), analytics in #7295 and release planning
   in #7628. Missing services produce truthful unavailable states, not a save gate.
6. Deliver vertical increments through existing gateway/backend/frontend/CLI
   pipelines, each followed by separate deployed CLI and browser evaluations.

## Decisions requiring owner review

The owner confirmed that multiple Projects and initiatives are supported and
requested a simple shared-team experience. Product has one existing-team mapping;
creation assigns the caller's current primary team or sole membership, and bound
Projects inherit that team context. Product rows and workspaces are visible only
to current team members or verified platform administrators in the selected tenant.
Legacy teamless Products are admin-only until assigned. Routine edits use existing
write permissions, without owner approval or a Project privacy selector. Individual
run visibility remains the separate `Only me` / `Product team` choice in #8148.

Remaining recommendations for review: an accountable human owner; one current
Product per governed Project; bounded on-demand discovery rather than a new
collector; the versioned maturity/check map; and the retention policy in the
[database design](database.md). PranavSharma1000 reviews these choices. Rejection
amends this design before the affected implementation. #5411 must name the delivery
integrator, resolve roadmap/registry/module/feature catalog IDs and assign service
owners before dispatch. Suggested mappings in [delivery](delivery.md) are labels,
not invented registry identities.

## Validation and completion boundary

All examples are fictional. Run from the repository root with Python 3.11+ and
the packages used for this review:

```bash
python3 -m venv /tmp/adp-profile-design-check
/tmp/adp-profile-design-check/bin/pip install PyYAML==6.0.3 jsonschema==4.26.0 openapi-spec-validator==0.7.2
/tmp/adp-profile-design-check/bin/python docs/product-onboarding/design/validate_design.py
/tmp/adp-profile-design-check/bin/python docs/product-onboarding/design/audit_inventory.py
python3 scripts/check-public-docs.py
git diff --check
```

The validator checks schemas, positive/negative examples, local references,
operation mappings and requirement/inventory coverage. Inventory reproduction
reads the pinned Git tree, not a possibly newer working tree. These checks are
design checks only: no database migration, deployed browser journey, installed CLI
journey, provider operation or deployed authorization behavior is proved here.
The separate [prototype browser checks](../mockups/product-onboarding.md#placement-and-verification)
exercise local interactions and validate simulated requests/responses against these
schemas, including save recovery, Project isolation and shared team membership. The design
story completes only after reviewed merge; the executor's handoff is a ready PR.

Remaining external facts: deployment target/role/access and provider entitlements
have not been verified; Context/L5 consumer API versions and the release integrator
are not yet assigned. They block the affected integration/live checkpoints, not
review of the profile core. See [source-audit coverage limits](source-audit.md#coverage-limits).
