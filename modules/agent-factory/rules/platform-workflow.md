# Shared platform workflow

## Adaptive Workflow Principle
For code delivery: implement and validate on the task branch, open a ready PR,
then review. Do not create draft PRs or request reviews of incomplete slices.
Use branch/commit links for progress. Existing drafts enter review only when
implementation and pre-submit checks are complete and the author marks them ready.

The workflow adapts to the work, not the other way around.

The AI model intelligently assesses what stages are needed based on:
1. User's stated intent and clarity
2. Existing codebase state (if any)
3. Complexity and scope of change
4. Risk and impact assessment

## Issue contracts

For issue authoring, refinement and acceptance, load
[agents/issue-authoring.md](agents/issue-authoring.md) and use
[templates/developer-issue.md](templates/developer-issue.md). The issue's explicit
completion boundary and named owners determine whether the assignment ends at
review, merge or live verification; generic phase/persona defaults below do not
replace that contract. Separate pre-review checks from post-merge checks so a
main-only evaluation does not prevent its enabling PR from being reviewed.
This adds no approval gate and does not bypass existing deployment authority.

