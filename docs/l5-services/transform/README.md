# AWS Transform: Product-scoped modernization

The [canonical architecture](architecture.md) and [seven-story backlog](backlog.md)
define bounded refactoring and dependency/runtime modernization for any onboarded
Product/project. **Design approved; runtime/live acceptance remains pending.** See [review record](review.json).
The service returns the provider-produced patch, exact source provenance and
independent before/after tests; publication of a PR is separately authorized.

- [Repository audit](audit.md): exhaustive pinned tracked-tree, build/deployment,
  SQL and related-PR inventories, plus unresolved semantic coverage limits.
- [Shared L5 architecture](../architecture.md) and [contracts](../contracts/v1/README.md).
- [Original proposal](https://github.com/aws-e/adp/blob/35e00861a273b4a6492e05c57917c0272947255d/docs/l5-services/transform/README.md),
  [capability expansion](https://github.com/aws-e/adp/blob/35e00861a273b4a6492e05c57917c0272947255d/docs/l5-services/capability-expansion.md)
  and [extracted POC evidence](https://github.com/aws-e/adp/blob/35e00861a273b4a6492e05c57917c0272947255d/docs/l5-services/poc-evidence.md)
  remain historical sources from open PR #9080, not assumed merged dependencies.

Runtime belongs in `modules/L5-services/` (`l5_services`). No provider jobs,
deployment, credential or billing changes are part of this design PR.

Shared context: [capability expansion](../capability-expansion.md) and
[extracted POC evidence](../poc-evidence.md). The architecture supersedes the initial
proposal summary while retaining its immutable evidence links.
