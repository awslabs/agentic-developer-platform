# Agent updates that humans can follow

Agent comments should explain the capability or problem, what is true now,
why that matters, and who acts next. Readers should not need to remember story
codes, internal component names or the previous run to understand a decision.

The shared [writing policy](../modules/agent-factory/rules/personas/shared/human-communication.md)
is the source of truth. It complements role-specific instructions in
[the persona directory](../modules/agent-factory/rules/personas/). It changes
presentation, not authority, acceptance criteria or approval gates.

## Expected behavior

- Lead with the result or decision, keeping blockers and missing checks visible.
- Explain internal names and decision codes before using them as shorthand.
- Distinguish a design, an open PR, merged code, deployment and verified acceptance.
- Give decision makers the recommendation, tradeoff, exact reply and approval scope.
- Put long test matrices, commands and investigation history below the summary or
  in linked evidence. Keep required handoff learnings in their separate record.
- Update when something changes; avoid repeating the full report at run shutdown.

For a small assessment answerable from supplied material, the worker returns
the answer directly without a separate plan comment, broad repository scan or
unrequested specification. The architect has an explicit bounded-assessment
mode for this. Implementation reviews still inspect relevant code and schema;
required AIDLC plan artifacts and approval gates remain in place.

The runtime publishes the final response as the issue outcome. Assessment
personas must not also post a full assessment through a comment tool and then
return a paraphrased recap. Required PR reviews and gates retain their own
destinations; the final response links to them with a brief status and next action.

Keep routine scope footers and bookkeeping in the run record unless requested or
consequential. Internal records are file writes; a clean Git diff does not support
“no files changed”. Label supplied or hypothetical evidence in the opening and
preserve its component names, roles and scope. Omit unnecessary inferences rather
than adding caveats around them. Keep missing checks and approval limits visible.

A recommendation does not settle the owner's choice. Add a follow-up question
only if different answers would change the recommendation or the user's requested
next step, and explain that dependency. Defer later design questions otherwise.
Qualify effort and schedule beside the recommendation, including a recommendation
to ship by a date; possible code reuse alone does not establish an estimate.
Stop once the conclusion, necessary support and next action are clear. With no
review target, the useful response is the verdict, evidence limit and next handoff.

Reviewers lead with verdict, blockers and user impact. Architects lead with
readiness and decisions. Operations and evaluations name the environment and
separate passed, failed, skipped and not-run checks. Product and intent agents
use recognizable user scenarios. PM/orchestrator updates explain capability
progress and dependencies. Exception diagnosis remains advisory. Delegated
Codex output uses the same writing principles without gaining supervisor duties.

For example, instead of:

> Task Complete. T2b merged; T3–T6 in review. Dispatch failed.

write:

> Member assignment is merged. Four related changes are still in review, so the
> full organization-management flow is not ready. The next worker could not be
> started; the orchestration owner needs to resolve that dispatch failure before
> work can continue. Deployment and browser acceptance have not been checked.

The latter statement is an example format, not a report of current repository status.

## Runtime reporting

The worker edits its live comment into one complete outcome report. It retains
late caveats instead of copying the first 500 characters. Parent-issue notices
link to that report, and the Python finalizer links to it through the existing
result-metadata bridge. If the live comment cannot be published, the worker
uses the existing GitHub/S3 reporting fallback.

The live display reports only observable worker stages: setup and the role's
run. A successful process exit does not mark implementation, verification or
PR creation complete. Skipped and unrecorded stages remain distinct, elapsed
time comes from the run clock, and finalization waits for outstanding progress
updates so they cannot overwrite the outcome.

The Python wrapper describes the Git facts it observes: an existing PR,
remaining work pushed, or review transcripts archived without a PR. A clean
local checkout no longer produces “no changes needed.” Invocation status enums,
correlation markers, PR marker backfills and existing gates are retained.

Fallback gates and reminders include mention-prefixed replies and stage-specific
approval consequences. The final loop proposal explicitly covers execution
scope and target environment; skipping it does not authorize construction.
Fallback artifact links point to a clean, tracked commit confirmed present on
GitHub. When publication cannot be verified, the comment says so rather than
claiming that artifacts were committed and published. A reminder links to the
current gate for its artifact revision and decision brief.

## Loading and packaging

| Entry point | Policy loading |
|---|---|
| Hosted worker and PM | After persona, phase and memory rules; independent of repository persona overrides. PM quick tasks also load it directly. |
| TypeScript chat | Once in final prompt composition, after recalled context |
| Legacy Python gateway | Once for Markdown, YAML and fallback personas, before caching |
| Codex bridge | Once in temporary AGENTS.md, before repository text so long repository instructions cannot truncate it away |

The policy lives under `rules/personas/shared/`, so the existing recursive
persona staging and worker/chat image copy paths include it. It is not listed
as a selectable persona. The legacy Python image now uses the module root as
its build context, matching its need to copy shared personas:

```sh
cd modules/agent-factory
docker build -f gateway/Dockerfile -t adp-agent-gateway .
```

## Verification and rollout

Regression tests cover prompt assembly with repository overrides, packaging,
Codex restoration of the repository's AGENTS.md, accurate duration and stage
states, late caveats, failed comment publication, self-pushed changes, transcript
archival, gate reply syntax, artifact publication and final-gate approval scope.
Gate/workflow tests use mocked APIs; they do not post to GitHub or start workers.

The targeted TypeScript tests and build pass. The relevant Python suites pass
apart from two existing entrypoint tests, reproduced on the unchanged revision:
`TestVaultClient.test_get_secret` expects the old secret prefix, and
`TestEntrypointMain.test_full_sequence_success` lacks the required queue setting.
The optional shellcheck tests skip when shellcheck is absent from PATH; a separate
`uvx --from shellcheck-py shellcheck` run passes. Python lint and TypeScript
compilation pass. Local tests do not establish
human comprehension or verify a deployed worker image.

After release through the existing worker/chat deployment process, review a
small sample across planning, review, implementation, operations and blocked
runs. Ask a reader to identify the outcome, current state, next owner/action
and approval consequence without opening another document. Check that critical
caveats remain visible and compare duplicate-comment counts with earlier runs.
These checks evaluate the intended improvement beyond prompt and renderer tests.

## Findings from the September 2026 pilot

Four assessment-only runs after PR #4953 exercised
[product #4954](https://github.com/aws-e/adp/issues/4954),
[architect #4955](https://github.com/aws-e/adp/issues/4955),
[reviewer #4956](https://github.com/aws-e/adp/issues/4956) and
[operations #4957](https://github.com/aws-e/adp/issues/4957). Their runtime notices
separated run completion from feature completion, but three personas posted a
separate assessment and repeated it in the final response. Product added an
unrequested decision; architect challenged a hypothetical premise using the
current codebase; operations strengthened synthetic test evidence into claims
about live exposure. Reviewer correctly withheld approval for a missing revision
but still produced a long, repeated answer.

Those observations motivated the bounded-assessment path, explicit single-outcome
instructions, bookkeeping check and evidence rules above. Regression checks verify
prompt assembly and existing workflow contracts; they cannot establish whether
models follow the new wording. After release, repeat the same four scenarios in
fresh issues, verify the installed prompt revision and compare whole-thread
repetition, readability and evidence accuracy. Keep necessary caveats visible;
shorter text alone is not success. These four single runs provide neither an A/B
comparison nor validation of all personas, AIDLC gates or chat conversations.

### Second pilot and further prompt tuning

After PR #4958, the same scenarios ran in fresh issues:
[product #4959](https://github.com/aws-e/adp/issues/4959),
[architect #4960](https://github.com/aws-e/adp/issues/4960),
[reviewer #4961](https://github.com/aws-e/adp/issues/4961) and
[operations #4962](https://github.com/aws-e/adp/issues/4962).
Separate plan comments fell from four to zero and duplicate assessments from
three to zero. Whole-thread substantive words fell from 7,310 to 1,514; final
answers alone fell 24.9%. All four reached the expected central conclusion, but
only architect passed the full unchanged rubric.

Product still bundled a later design decision and overstated an option's unique
benefit. Reviewer correctly withheld approval but claimed no files changed after
writing an allowed internal record. Operations inferred an unspecified component's
purpose and expanded its deployment status into a claim about the user journey.
The next prompt revision tightens the shared evidence and decision rules, the
worker's final check and those three personas. The architect persona is unchanged.
These edits omit unnecessary assumptions, questions and scope footers while
preserving required checks, reviews and approvals. They do not truncate responses.

The second pilot covers four single runs, not a controlled A/B comparison or a
human-reader study. Prompt-loading and workflow regression checks cannot establish
compliance with this further tuning; a fresh live pilot after release is still
needed. Preserve the same stimuli and rubric for comparison, and separately use
new scenarios to check whether the evidence and decision rules generalize.
