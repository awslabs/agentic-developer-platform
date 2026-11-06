# Author epics and stories for traceable tests

The canonical human/agent instructions are the
[issue-authoring guide](../../modules/agent-factory/rules/agents/issue-authoring.md#requirements-lineage-contract-epics-and-stories).
Use its single contract rather than a separate Insights-specific issue format.
The guide is shipped to workers; GitHub exposes copies of the canonical templates.

| Authoring task | Start here |
|---|---|
| Epic | [Canonical epic body](../../modules/agent-factory/rules/templates/epic-issue.md) / GitHub **Engineering epic** template |
| Story | [Canonical developer body](../../modules/agent-factory/rules/templates/developer-issue.md) / GitHub **Developer task** template |
| Filled example | [Organization epic](examples/epic-organizations.md) and [invitation story](examples/story-invitations.md) |
| Data contract | [Versioned lineage](lineage-schema.md#11-requirements-and-test-analytics-v2) |

Examples are fictional. Their API and selectors are proposed, not claims about
ADP's current organization behavior or implemented tests. Epic/story filing and
native hierarchy creation remain human or explicitly authorized agent actions.
No issue-event workflow is installed by this change.

## The authoring sequence

1. Inspect the existing capability, test catalog and parent initiative. Write
   the epic's outcome, scope, accountable owner and completion boundary.
2. Bind to a registered project; use existing module/feature IDs. Keep unresolved
   references explicit while drafting. Labels do not grant project access.
3. Decompose into bounded stories, each with one acceptance table and stable
   `AC-01` keys. After filing, use provider-assigned IDs and native child links.
4. For each AC write **Given → When → Then**, an independent oracle, evidence,
   phase and owner. Include the relevant negative/boundary/recovery behavior.
5. Map each AC to scenarios and test references. Distinguish planned tests,
   implemented selectors and manual procedures; specify fixture, clock, random,
   async, isolation and cleanup controls where relevant. Preserve parameter cases.
6. Map each epic `EC-01` to qualified child AC references and direct integration
   evidence. An implementation child's merge cannot satisfy a live epic boundary.
7. Review the oracles and actual execution paths, then run the optional format
   check. Resolve the blockers for the next phase, retaining later-phase owners.
8. Implement and run tests; report exact requirements/source revisions,
   scenario/test/run/attempt outcomes and evidence. Preserve retries and gaps.

Issue bodies remain below 8 KiB. Put large matrices and procedures in versioned
files and link them at an immutable revision. Acceptance criteria stay in one
primary table. Scenario mappings refine cases rather than creating competing ACs.
Existing IDs are never renumbered to match the example's padding.

## Optional offline format check

From a checkout containing this change:

```bash
python3 modules/insights/authoring/validate_issue.py /path/to/issue-body.md --stage draft
python3 modules/insights/authoring/validate_issue.py /path/to/issue-body.md --stage ready
```

For an existing authorized issue, save its body with the GitHub CLI first:

```bash
gh issue view "$ISSUE_NUMBER" --repo "$ISSUE_REPOSITORY" --json body --jq .body > /tmp/issue-body.md
python3 modules/insights/authoring/validate_issue.py /tmp/issue-body.md --stage ready
```

The checker supports the six-section epic/story format only, not every historic
issue or the AIDLC intake form. It checks section/table structure, local ID
uniqueness and mapping references, required values, and (in ready mode) explicit
Given/When, scenario controls and reference syntax. Draft mode allows unresolved
values; ready mode permits **planned tests**, because implementation creates them.
Escaping literal pipes as `\|` keeps Markdown table cells unambiguous. Detailed
scenario/manual references use `path@<40-character-commit>#anchor`.

A successful check proves **format only**. It does not query the project registry,
confirm native GitHub relationships, check referenced selectors/procedures exist,
verify credentials/fixtures, judge the oracle or prove deterministic behavior.
Review those independently. It neither dispatches agents nor grants deployment
permission, marks issues ready, uploads snapshots, or records acceptance.
CI validates the checker and these examples, not live issue bodies automatically.

## How the PM agent receives the contract

The PM runtime includes the full guide and both templates in its model input,
including initial/resumed work, reassessment, quick tasks and monitoring actions.
The files come from the installed worker package, with a source-checkout fallback;
a missing or empty contract stops that inference call instead of silently omitting
the instructions. The PM persona points to the same sources. Codex PM includes
them in its versioned, digest-checked rule projection.

These changes take effect after the updated worker image is deployed. Codex PM
also needs its updated persona catalogue published through the existing release
process; existing admitted snapshots do not acquire new rules automatically.
This change does not perform deployment or persona registration.

Tests check the actual PM query input, installed-source failures and Codex
projection content. This ensures the instructions are supplied, not that a model
always follows them. Issue-write validation and dispatch readiness enforcement
remain separate work; the optional checker is not automatically invoked.

## How the AI-DLC agent receives the contract

The Claude AI-DLC worker loads the full installed authoring guide and both
templates in its rules, independently of repository persona overrides. It uses
the same loader as PM. Missing/empty installed authoring files stop prompt
assembly instead of silently omitting the contract.

The AI-DLC issue emitter uses the canonical epic and story templates. Its review
checklist covers project/module/feature bindings, stable EC/AC keys, scenario/test
mappings and deterministic controls. It fills actual child references after
filing, verifies native relationships and reports partial emission if links fail.
Draft aliases never stand in for provider-assigned issue identity.

The Codex AI-DLC persona carries a compact authoring checklist and references to
the canonical templates. Its existing phase instructions are substantial; loading
the full guide and both templates inline would exceed the Task request budget.
A fixture test captures the actual SDK request to verify that the authoring and
amendment restrictions are present within the existing bound. This is distinct
from the full guide/template injection used by the Claude worker.

The delivery-planning and loop-proposal approvals still control issue emission
and materialization. Amendment mode still produces only an inert draft; it cannot
create/edit issues, dispatch work or accept a plan. Supplying authoring instructions
does not add an automatic issue-write validator or change these approval gates.
The updated worker image and relevant Codex persona snapshot must be rolled out
before runtime behavior changes.

## What flows into Engineering Insights

| Authored information | Retained lineage |
|---|---|
| Project reference and native epic/story relationships | Resolve through registered source authority; copied body fields cannot grant scope |
| Story brief and primary AC table | Immutable source snapshot, `story_revision` and individually versioned `acceptance_criterion` records |
| Scenario/test mapping and versioned strategies | Catalog definitions and `coverage_mapping` records; retain issue-local aliases |
| Planned verification and implementation selection | Run/catalog/requirements/selection snapshots and `run_test` records |
| Each execution attempt | `test_result` plus exact artifact references |
| Human or policy verdict | Separately authorized `acceptance_assessment` |

The collector must preserve whether a test was planned, implemented or manually
verified; it must not turn a planned selector into executed coverage. The existing
v2 contract covers story/criterion/test evidence. Epic records and dated native
parent relationships remain part of the broader lineage design; an epic authoring
template does not add a deployed epic ingestion schema or rollup service.

Automatic issue ingestion, AC extraction, snapshot publication, source-reference
resolution and acceptance evaluation are still implementation work. Until then,
keep explicit reviewed mappings and revision references with the run evidence.
The authoring rules make that future integration well-defined; they do not claim
that submitting a template already populates S3 or DuckDB.
