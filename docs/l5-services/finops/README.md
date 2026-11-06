# AWS FinOps Agent

Design for scoped native cost inquiries and opt-in periodic reports for any
onboarded Product/project. **Design approved; runtime/live acceptance remains pending.** See [review record](review.json).

- [Canonical architecture F1](architecture.md): billing isolation, configuration,
  native inquiry/recovery, independent reconciliation, clients and entitled reuse.
- [Seven linked implementation stories](backlog.md): FO01–FO07, ownership,
  EC-to-AC mapping and independent test procedures.
- [Complete repository audit](audit.md): tracked artifacts, build/deployment entry
  points, migrations, open-work overlap and explicit coverage limits.
- [Draft payload contract](contract.json) and [synthetic examples](examples.json).
- [Verification and handoff](verification.md): checks, review and placement limits.
- [Compatible release and rollback](runbook.md): staged admission, worker readback
  and private live-evidence procedure.

Historical baseline: [service proposal](https://github.com/aws-e/adp/blob/35e00861a273b4a6492e05c57917c0272947255d/docs/l5-services/finops/README.md),
[common extension design](https://github.com/aws-e/adp/blob/35e00861a273b4a6492e05c57917c0272947255d/docs/l5-services/capability-expansion.md)
and [extracted POC evidence](https://github.com/aws-e/adp/blob/35e00861a273b4a6492e05c57917c0272947255d/docs/l5-services/poc-evidence.md)
in open PR #9080. #7923 reports a successful native console inquiry and scoped CE
comparison/cleanup. Later #7981/#8015 limits do not erase that result, and CE-only
rows do not establish native integration or durable automation.

Follow the [shared architecture](../architecture.md) and [contracts](../contracts/v1/README.md).
Runtime belongs in `modules/L5-services/`; this subtree contains design artifacts
and offline verification tools only. Configuration, connection references and
source context never grant billing access. No provider setup or deployment is
performed by this design. All seven children are in Roadmap project 4; coordinator readback
verified membership on 2026-10-27.

Shared context: [capability expansion](../capability-expansion.md) and
[extracted POC evidence](../poc-evidence.md). The architecture supersedes the initial
proposal summary while retaining its immutable evidence links.
