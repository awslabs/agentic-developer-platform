# Review with a shared GitHub identity

ADP supports using the same GitHub App for developer and reviewer workers.
GitHub refuses a formal approval on a PR authored by that same identity, so
legacy story completion also accepts the Codex reviewer's existing PR comment:

```text
## agent-codex-reviewer — APPROVE

**Reviewed head:** `<full current commit SHA>`
**Blockers:** 0
**Engine:** Codex SDK <version>
```

GitHub must attribute the comment to the tenant's configured App through
`performed_via_github_app.id`, with a bot author. The latest reviewer verdict
must approve the current head with zero blockers. The reviewer's
`FIXES PUSHED AND APPROVED` verdict is also accepted after its fresh review of
the repaired tree. Unrelated comments and issue-readiness verdicts do not count.
Later requested changes, stale or malformed verdicts, incomplete provider reads,
and unsuccessful checks prevent completion.

This authenticates the publishing App, not a separate GitHub person. Workers
sharing that App share its publishing authority. The durable review-cycle path
separately verifies developer and reviewer run identities and structured review
evidence; its runs must still differ. It no longer adds an implicit requirement
for one formal GitHub approval when the repository does not require one.

GitHub's configured approval requirements, code owner rules, CI requirements,
and merge restrictions still apply. This change does not bypass branch protection
or activate review orchestration on an existing legacy flow. No second App,
dynamic IAM roles, configuration switch, or database migration is required.

The stored hold code `no_independent_review` remains compatible; its explanation
now asks for verified approval of the current head rather than another GitHub
identity. Already-merged, bound stories are reconsidered by normal reconciliation.
