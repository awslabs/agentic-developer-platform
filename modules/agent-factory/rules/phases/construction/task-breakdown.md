# Task Breakdown - Shared Rule

## Purpose
Every story is delivered as the same kind of task board, whichever agent or model
is working it and however many processes it takes. The board is derived from the
issue's acceptance criteria, not invented per run, so two runs of the same story
produce the same skeleton, progress is comparable across stories, and a story
that is too big is visible on the first turn instead of after hours of work.

## Primary Agent
@agent-developer (creates and works the board) → @agent-reviewer (finishes and
verifies against the same board). The controller publishes the board; the model
only reports it.

## Scope
Applies to every implementation or repair assignment with a driving issue. The
controller never fails a run over this rule: deviations are reported back as
notes on the next turn. Follow the rule so those notes stay empty.

---

## Step 1: Derive the skeleton from the acceptance criteria

Start with a human-readable implementation plan: stable parent steps explaining
the behavior the user will gain. Group the detailed tasks below those steps.
For the structured board, every child carries `planStep: { id, title }`; children
of the same parent reuse both values. Parent titles use plain language, not
acceptance IDs, file paths or internal shorthand. Keep acceptance IDs on the
detailed children, which may span several criteria within one logical step.
Parent status is derived from its children and their required validation.
The UI shows parent steps first and lets the reader expand their details.

The canonical saved board is `.adp/tasks/<issue-number>.json` on the story
branch. In the Codex developer/reviewer runtime, the controller commits and
pushes it at checkpoints. Its `plan` lists the human-readable parent steps,
their child IDs and calculated progress; `tasks` holds the detailed work,
status, evidence and parent links. The UI and run records are views of these
checkpoints, not separate editable plans. Restore this file before replanning
on a retry. Parent totals are regenerated from the children; never edit them
independently or infer completion merely from a PR merge.

Read the issue's Validation table (stable IDs such as `AC-01`) or its bold
criterion markers (such as `DATA01`). For **each** acceptance ID, the board gets:

| From the issue | Task | Rule |
|---|---|---|
| Expected result (the positive path) | `<ID>-c1` `code` | Mandatory. Split into `c1..cN` only when each part is one package or component; at most four per criterion. |
| Required evidence | `<ID>-t1` `test` | Mandatory; `covers` every `<ID>-c*`. The title names the suite or command and the fixture type: unit, emulator/test instance, or live. |
| Each Impact-analysis failure row mapped to the ID | `<ID>-t2`, `<ID>-t3` … `test` | One negative-path test per failure row. A criterion about denial, isolation or rejection is never done on positive tests alone. |
| A cross-component contract in the row | `<ID>-c-wire` `code` | Explicit wiring/integration task: new code must reach the live path, not sit beside it. |
| Phase or owner says after deploy / live evidence | the task, status `blocked`, note starting `deployed-target:` plus the owner | It cannot be done from a pod. Keep it visible; never attempt it, never drop it, never count it done. |
| A harness, fixture or CI job a test cannot run without | `<ID>-i1` `infra` | Only when a named task needs it; `covers` that task. No speculative infrastructure. |

Every task carries `criterion` = the acceptance ID it serves. A task with no
criterion is a smell; an acceptance ID with no tasks is a gap the controller will
point out.

## Step 2: Size each task

A task is one sitting of work that can be finished and verified in the pod:
one behavior change in one package or component, roughly 300 changed lines at
most, with its covering test runnable by one command. Titles are verb + object
("authorize by-id reads against session scope"), not categories ("auth work").
`files` lists the primary paths once known.

## Step 3: Order and work the board

Board order is the work order: criteria in issue order; within a criterion, code
before the tests that prove it; `infra` only immediately before the task it
unblocks. Each turn takes exactly the first open task to `done`, together with
its covering test, runs the tests for the packages it changed, commits with the
task id (`feat(#<issue> AC-02-c1): …`, `test(#<issue> AC-02-t1): …`), pushes and
keeps the ready PR current. A code task is `done` only when a done test covers
it. Full suites run once, when the whole board is done.

## Step 4: Recognise an oversized story

If the skeleton has more than twelve `code` tasks, or more than two criteria
whose tasks are `deployed-target`, say so in the first turn's summary: the story
should be split into child issues, one per criterion group, before more budget
is spent. Continue with the first tasks anyway; the decision to split belongs to
the issue owner, and the controller surfaces the signal on the issue.

## Step 5: Keep the board truthful

Do not mark a task done to shorten the list, do not merge tasks to hide
scope, and do not add tasks that serve no acceptance ID. When a previous process
left a board in the branch, it is the plan: keep its ids, resume at the first
open task, and change the skeleton only for a reason you state in the task note.
Resume an in-progress task before starting a new one. Retain the parent links,
completed tasks, evidence and blocked tasks. Add the two-level grouping to an
older flat board without resetting its progress. Persist the active child and
every completed checkpoint through the runtime's existing board mechanism.
