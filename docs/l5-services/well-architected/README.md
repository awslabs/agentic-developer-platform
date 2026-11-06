# AWS Well-Architected Agent: Product architecture reviews

**Approved implementation design:** [architecture.md](architecture.md);
[accepted revision and actual review](review.json).
The service reviews selected immutable IaC/documents and Product goals through
AWS Agent Profiles and finite recommendation generations. It does not inspect
cloud resources implicitly, purchase Support, certify compliance or remediate.
Runtime is proposed under `modules/L5-services/`, extending the shared L5 API and
workers for any onboarded Product/project without redeployment per onboarding.

- [Architecture](architecture.md): configuration, identity, provider lifecycle,
  SQL, provenance, cancellation/recovery, client parity and rollout ownership.
- [Implementation backlog](backlog.md): WA01–WA07, native children, acceptance and
  scenario mapping, dependencies and advisory file groups.
- [Complete source audit](audit.md): tracked artifacts, deployment/build entries,
  migration graph and explicit exclusions at the inspected main revision.
- [Verification and handoff](verification.md): checks, review state and remaining
  live prerequisites. Design publication is not implementation acceptance.

The [service proposal at PR #9080's extracted revision][proposal] and
[POC evidence][poc] remain historical context. #7922/#7980 reported Support
failures; #8014 prepared source/IaC without a native generation. No native
recommendations or current entitlement are inferred from those reports.
The [shared architecture](../architecture.md) and
[v1 contracts](../contracts/v1/README.md) remain common-core owners; this service
proposes coordinated extensions instead of overwriting sibling designs.

Offline checks (repository root, Python 3.12+, PyYAML, jsonschema and
openapi-spec-validator; no cloud credentials):

```sh
python3 docs/l5-services/well-architected/audit_inventory.py --check
python3 docs/l5-services/well-architected/validate_design.py
python3 docs/l5-services/contracts/v1/validate.py
python3 scripts/check-public-docs.py
```

[proposal]: https://github.com/aws-e/adp/blob/35e00861a273b4a6492e05c57917c0272947255d/docs/l5-services/well-architected/README.md
[poc]: https://github.com/aws-e/adp/blob/35e00861a273b4a6492e05c57917c0272947255d/docs/l5-services/poc-evidence.md

Shared context: [capability expansion](../capability-expansion.md) and
[extracted POC evidence](../poc-evidence.md). The architecture supersedes the initial
proposal summary while retaining its immutable evidence links.
