# L5 Services

Architecture and contracts for Product-configured services, initially Security
threat modeling and static code review, with DevOps and further service extensions.
Implementation artifacts exist, but live availability remains unverified and
requires authorized deployment.
The Python distribution is `adp-l5-services`, imported as `l5_services`.
Gateway hosts the HTTP API and shared clients; this module owns domain logic,
state, migrations, workers, adapters, infrastructure and tests.

This folder is the canonical home for L5 documentation, contract specifications,
audit evidence and their offline validation tools. Runtime implementation belongs
in [modules/L5-services](../../modules/L5-services/README.md).

- [FinOps canonical design and backlog](finops/README.md): scoped native cost inquiries, complete repository audit and seven implementation stories (design approved; live acceptance pending).
- [Modular deployment contract](modular-deployment-design.md): independent roots, CI/CD, private inputs and safe state migration.
- [Canonical architecture](architecture.md): decisions, boundaries and open prerequisites.
- [Version 1 contracts](contracts/v1/README.md): API, data, examples and client parity.
- [Implementation backlog](backlog.md): all 16 existing Security stories; no new dispatch.
- [Security deployment and rollback](deployment.md): explicit worker publication, pause, recovery and retained resources.
- [Product workspace Security user guide](user-guide.md): configuration, explicit provider work, results and recovery.
- [Autonomous delivery waves](delivery-plan.md): grouped development, normal auto-deploys and one coordinated L5 release.
- [Story file plan](story-files.md): files to create, existing files to modify, predecessor-owned files and shared edit coordination for each node.
- [Repository audit](audit.md): complete tracked-artifact and entry-point inventory.
- [Consistency and verification](verification.md): evidence limits and acceptance mapping.
- [Well-Architected Agent architecture](well-architected/architecture.md): source-only native review design, seven-story backlog and current complete audit (design approved; live acceptance pending).
- [DevOps Agent configuration manual](devopsagnent/configuration/README.md): AWS setup steps, existing CLI operations and the Product Profile configuration mapping.
- [DevOps qualification with human UI observations](devopsagnent/manual-qualification.md): original user submission and ADP SSO evidence while browser automation remains disabled.
- [DevOps Agent implementation backlog](devopsagnent/backlog.md): common architecture, 19 stories, shared Security dependencies, delivery waves and advisory file groups.
- [Transform, Well-Architected and FinOps expansion](capability-expansion.md): proposed service epics, shared architecture, Profile ownership and grouped delivery.
- [Transform architecture and backlog](transform/README.md): bounded modernization, shared execution and seven native stories.
- [Extracted POC evidence](poc-evidence.md): eight historical stories, run records, immutable report/helper sources and observed capability limits.

Approval and delivery status are recorded per service. Design approval does not
establish deployment or live acceptance; Security acceptance does not complete
other L5 services in [the parent epic](https://github.com/aws-e/adp/issues/8150).

Offline checks from the repository root (Python 3.12+, PyYAML and jsonschema):

```sh
python3 docs/l5-services/audit_inventory.py --check
python3 docs/l5-services/contracts/v1/validate.py
python3 scripts/check-public-docs.py
```

These commands make no cloud/provider calls. Shared runtime code now exists under
`modules/L5-services/`; the expansion design records an inspected source baseline.
Individual proposals and historical reports do not prove deployment or acceptance.
Production setup requires the existing
[deployment authority and playbook](../adp-platform-deployment/deploy-with-agent.md).
