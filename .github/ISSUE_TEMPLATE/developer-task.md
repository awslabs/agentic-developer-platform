---
name: Developer task
about: A bounded implementation or build-and-run task with observable acceptance criteria
title: ""
labels: ""
assignees: ""
---

<!-- Copy this body for an implementable story, defect, or build-and-run task.
Replace prompts; remove inapplicable rows with a short reason. Keep acceptance
in one table. Authoring guidance: ../../modules/agent-factory/rules/agents/issue-authoring.md.
Do not add agent mentions/trigger labels when filing. -->

## The problem in plain terms

<Who does what, what happens today, and what outcome they need. No code paths.>

**The fix in one line:** <Observable change.>

## Description

<One bounded outcome and why it matters.>

- **Parent / reports to:** <Issue links, or standalone.>
- **Delivery owner:** <Named executor or role.>
- **Completion boundary:** <Reviewed implementation / merged implementation /
  deployed and live-verified; choose one and identify any handoff owner.>
- **In scope:** <Capabilities delivered here.>
- **Out of scope:** <Adjacent capabilities explicitly deferred, with links.>

## Impact analysis

- **Users and surfaces affected:** <Callers, UI/CLI, tenants, billing or ops.>
- **Cost / quota bound:** <Applicable resource/request/time limits, or none.>

| Failure | User or operational impact | Acceptance ID |
|---|---|---|
| <Relevant failure> | <Concrete effect> | AC-02 |

## Design

### Starting point and reuse

| Existing component / PR | Path or evidence link and checked revision | Reuse / change |
|---|---|---|
| <Component> | <Verified reference> | <Purpose> |

### Implementation contract

- **Deliverables and entry points:** <Existing paths to change; proposed new
  paths marked proposed. Paths guide implementation; behavior defines scope.>
- **Inputs and configuration:** <Names, sources, defaults/required values,
  precedence and invalid-input behavior. Separate example values from constants.>
- **Outputs and behavior:** <CLI/API/UI result, error cases, retry/rerun semantics.>
- **Compatibility and data:** <Existing callers, schema/migration and auth/scope
  effects; relevant fresh/existing/pending states and conflicting inputs. For
  multi-step flows, name the shared schema, producer/consumer identity, real
  caller handoff and integration owner.>

### Prerequisites and unresolved facts

| Item | Status and evidence/date | Owner and next action | Blocks which step? |
|---|---|---|---|
| <Dependency/access/decision, or none> | <Verified / unverified / unavailable> | <Specific action> | <Implementation / deployment / acceptance> |

<Bound any required investigation: question, owner, artifact it must produce,
and the step that depends on the answer. Routine implementation choices remain
with the developer. Do not present an unknown as a verified prerequisite.>

## Deployment

- **Environment and identity:** <Target selection/config, credential reference
  and execution location; no secret values.>
- **One-time setup:** <Artifact/command, owner, permissions; or none.>
- **Per-run work:** <What is created, used, removed and retained; or none.>
- **Integration / rollout:** <Verified workflow + trigger; distinguish automatic
  from manual. Mark commands/interfaces still to be built as proposed.>
- **Recovery:** <Rollback/cleanup/resume and its owner, where applicable. For
  partial success, name the durable retry record, automatic trigger and retirement
  condition; cover acknowledgement success with reporting failure and restart.>
- **Handoff / stop condition:** <Who owns post-merge work and what evidence closes
  this issue. On an external blocker, provide exact owner/action and continue
  independent work. Preserve existing approvals; filing is not deployment approval.>

## Validation

| ID | Setup and action | Expected result / failure condition | Required evidence | Phase and owner |
|---|---|---|---|---|
| AC-01 | <Happy path; command or precise scenario> | <Observable assertion> | <Test/run/artifact> | <Before review / after deploy; owner> |
| AC-02 | <Relevant failure or boundary case> | <Observable rejection/recovery> | <Test/run/artifact> | <Phase; owner> |
| AC-03 | <Existing flow or rerun, when relevant> | <Compatibility/idempotence assertion> | <Test/run/artifact> | <Phase; owner> |

**Execution:** <Commands and check names, existing or proposed; required live
fixtures and evidence source if applicable. Trace critical ACs through the real
entry point/installed worker to the assertion; name a plausible wrong result
that must fail. Split compound proof into explicit subclaims. For broad
automation, name the first runnable checkpoint and remaining full acceptance.
Link deep procedures at a revision.>

**Finding closure owner:** <Who fixes review defects and integrates the final
revision. Consolidate findings; direct in-scope fixes may be made by an authorized
review/delivery owner. Keep substantial missing work explicit.>

**Completion report:** Map every AC ID to pass/fail/blocked/not-run and evidence
at the tested revision. State remaining rollout/handoff work. A required blocked
or not-run row is incomplete; mocks do not establish a live acceptance claim.
Distinguish missing implementation from an implemented check blocked by a fixture.
