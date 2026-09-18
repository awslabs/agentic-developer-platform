/**
 * Tests for the execution presentation projection — issue #5145.
 *
 * Every case here is a way the panel could render perfectly and still lie. The
 * issue lists them as requirements because each has an obvious optimistic
 * implementation: treat absence as success, treat blocked as failed, treat a
 * finished worker as accepted delivery, treat an unobserved outcome as resolved.
 * A rendering test would catch none of them, because in each case something
 * plausible appears on screen.
 */
import { describe, it, expect } from 'vitest';
import {
  absentExecutionNote,
  actionDetail,
  executionForNode,
  executionPresentation,
  ownerLabel,
  phaseLabel,
  relativeAge,
  relativeDue,
} from '@/utils/executionProgress';
import type {
  ExecutionAction,
  ExecutionSummary,
  FlowExecution,
} from '@/types/orchestration';

const SERVER_TIME = '2026-09-18T12:00:00+00:00';

function action(overrides: Partial<ExecutionAction> = {}): ExecutionAction {
  return {
    id: 'action-1',
    operation_key: 'pr:story-a:cycle-1',
    kind: 'open_pull_request',
    status: 'succeeded',
    attempt: 1,
    resolved: true,
    artifact_ref: 's3://adp-artifacts/story-a/patch.diff',
    receipt_ref: 'pr/PR_kwDOABCD1234',
    created_at: '2026-09-18T11:30:00+00:00',
    observed_at: '2026-09-18T11:31:00+00:00',
    ...overrides,
  };
}

function execution(overrides: Partial<ExecutionSummary> = {}): ExecutionSummary {
  return {
    id: 'exec-1',
    node_id: 'node-1',
    cycle: 1,
    phase: 'delivering',
    status: 'runnable',
    revision: 3,
    attempts: 1,
    next_check_at: '2026-09-18T12:15:00+00:00',
    deadline_at: null,
    progressed_at: '2026-09-18T11:55:00+00:00',
    progress_note: null,
    block: null,
    pending_action_key: null,
    notification_receipt_ref: null,
    handoff_receipt_ref: null,
    created_at: '2026-09-18T11:00:00+00:00',
    updated_at: '2026-09-18T11:55:00+00:00',
    actions: [],
    action_overflow: false,
    ...overrides,
  };
}

describe('absence is never success', () => {
  it('says a legacy flow has no execution record, not that it succeeded', () => {
    // Every flow delivered before the ledger existed has no row, permanently. The
    // wording has to name that, because "nothing outstanding" is how an operator
    // reads a blank panel.
    const note = absentExecutionNote(true);

    expect(note.headline).toMatch(/no execution record/i);
    expect(note.headline).not.toMatch(/complete|success|delivered|passed/i);
    expect(note.tone).toBe('unknown');
  });

  it('distinguishes "not started" from "no record at all"', () => {
    // Two different absences. A node in a tracked flow that has not run yet is
    // pending; a flow predating the ledger will never have a record. Conflating
    // them either invents a delivery-tracking gap or hides one.
    const notStarted = absentExecutionNote(false);

    expect(notStarted.headline).toMatch(/not.*recorded yet|no delivery activity/i);
    expect(notStarted.tone).toBe('pending');
    expect(notStarted.headline).not.toBe(absentExecutionNote(true).headline);
  });
});

describe('blocked is not failed', () => {
  const blocked = execution({
    status: 'blocked',
    phase: 'settling',
    block: {
      code: 'human_gate_required',
      owner: 'platform-operator',
      required_input: 'approve the wave gate for story-a',
      remaining_gates: ['gate:security-review', 'gate:cost-approval'],
      progressed_at: '2026-09-18T09:00:00+00:00',
      detail: null,
    },
  });

  it('renders as attention, never as an error tone', () => {
    const view = executionPresentation(blocked, SERVER_TIME);

    expect(view.tone).toBe('attention');
    expect(view.headline).toMatch(/blocked/i);
    // Not "failed": a block means someone must supply something, and the word
    // "failed" sends an operator hunting a crash that never happened.
    expect(view.headline).not.toMatch(/fail/i);
  });

  it('names who acts and what they supply', () => {
    // The whole point of the typed block. A bare "blocked" flag leaves an operator
    // reading logs that expire.
    const view = executionPresentation(blocked, SERVER_TIME);

    expect(view.blockOwner).toBe('A platform operator');
    expect(view.blockRequiredInput).toBe('approve the wave gate for story-a');
  });

  it('lists every outstanding gate', () => {
    const view = executionPresentation(blocked, SERVER_TIME);

    expect(view.remainingGates).toEqual(['gate:security-review', 'gate:cost-approval']);
  });

  it('ages a block from its own last progress, not the block moment', () => {
    // `progressed_at` on the block is the last REAL progress — the ledger does not
    // reset it when a row blocks. Using the blocking write's time instead would
    // reset the clock that separates "stuck for a minute" from "stuck since
    // Tuesday", on exactly the rows where the answer matters.
    const view = executionPresentation(blocked, SERVER_TIME);

    expect(view.lastProgress).toBe('3 hours ago');
  });

  it('falls back to an unrecognised owner verbatim rather than dropping it', () => {
    // A newer engine's owner value must still be actionable. Mapping it to a
    // familiar-looking default would route an operator to the wrong person.
    expect(ownerLabel('release-manager')).toBe('release-manager');
  });
});

describe('a finished worker is not accepted delivery', () => {
  it('keeps a successful action from implying the cycle is complete', () => {
    // The case the issue names: the worker finished, review/merge/deploy have not.
    const stillBlocked = execution({
      status: 'blocked',
      phase: 'awaiting_review',
      actions: [action({ status: 'succeeded', resolved: true })],
      block: {
        code: 'human_gate_required',
        owner: 'platform-operator',
        required_input: 'approve the merge gate',
        remaining_gates: ['gate:merge-approval'],
        progressed_at: '2026-09-18T11:55:00+00:00',
        detail: null,
      },
    });

    const view = executionPresentation(stillBlocked, SERVER_TIME);

    expect(view.tone).toBe('attention');
    expect(view.remainingGates).toEqual(['gate:merge-approval']);
    expect(view.tone).not.toBe('complete');
  });

  it('does not call a concluded cycle complete when an outcome was never confirmed', () => {
    // `concluded` is reached by a cycle that gave up as well as one that delivered.
    const unconfirmed = execution({
      status: 'concluded',
      phase: 'concluded',
      next_check_at: null,
      actions: [action({ status: 'unknown', resolved: false, receipt_ref: null })],
    });

    const view = executionPresentation(unconfirmed, SERVER_TIME);

    expect(view.tone).toBe('unknown');
    expect(view.headline).toMatch(/never confirmed|not confirmed/i);
  });

  it('flags a concluded cycle that contains a failed step', () => {
    const withFailure = execution({
      status: 'concluded',
      phase: 'concluded',
      next_check_at: null,
      actions: [action({ status: 'failed', resolved: true })],
    });

    const view = executionPresentation(withFailure, SERVER_TIME);

    expect(view.tone).toBe('attention');
    expect(view.headline).toMatch(/failed step/i);
  });

  it('calls a concluded cycle complete only when the graph says the node was accepted', () => {
    // The one case where "complete" is the honest word, asserted so the guards above
    // cannot be satisfied by never saying it at all. Note the fourth argument: the
    // graph's accepted state is what earns the tone, not the execution row.
    const clean = execution({
      status: 'concluded',
      phase: 'concluded',
      next_check_at: null,
      actions: [action({ status: 'succeeded', resolved: true })],
    });

    expect(executionPresentation(clean, SERVER_TIME, 0, true).tone).toBe('complete');
  });

  it('never says complete when the worker succeeded but the graph has not accepted the work', () => {
    // THE discriminating fixture. A succeeded worker action on a concluded execution
    // whose graph node is still awaiting review — which is the ordinary state of a
    // story with an open PR, not an edge case.
    //
    // `concluded` is a SCHEDULER statement ("no further pickup", per
    // `execution_state.py`), not a delivery statement, and it is reached by a cycle
    // that gave up — attempts exhausted, deadline passed, budget spent — just as
    // readily as by one that delivered. Giving up leaves no failed action behind,
    // because abandoning work is not a failed step, so every other guard in this
    // describe block passes on it. Only the graph's own verdict separates the two.
    const workerSucceeded = execution({
      status: 'concluded',
      phase: 'concluded',
      next_check_at: null,
      actions: [action({ status: 'succeeded', resolved: true })],
    });

    // `false` — the graph node is NOT passed (still awaiting review/deploy/eval).
    const view = executionPresentation(workerSucceeded, SERVER_TIME, 0, false);

    expect(view.tone).not.toBe('complete');
    expect(view.tone).toBe('pending');
    expect(view.headline).toMatch(/acceptance still pending/i);
  });

  it('defaults to pending, not complete, when the graph state is not supplied', () => {
    // The back-door version of the same bug: an optional input defaulting to
    // "accepted" would restore the false green at every call site that forgot to pass
    // it, and the fixture above would not catch it because it passes the state
    // explicitly. Absent authoritative state must read as pending.
    const clean = execution({
      status: 'concluded',
      phase: 'concluded',
      next_check_at: null,
      actions: [action({ status: 'succeeded', resolved: true })],
    });

    expect(executionPresentation(clean, SERVER_TIME).tone).toBe('pending');
    expect(executionPresentation(clean, SERVER_TIME, 0, undefined).tone).not.toBe('complete');
  });
});

describe('evidence and unknown outcomes', () => {
  it('preserves unknown as unknown, never as either outcome', () => {
    expect(actionDetail(action({ status: 'unknown' }))).toMatch(/could not be determined/i);
    expect(actionDetail(action({ status: 'unknown' }))).not.toMatch(/succeed|fail/i);
  });

  it('reads a missing receipt as pending, not as nothing having happened', () => {
    // Rendering nothing for a missing receipt makes "the deployment receipt never
    // arrived" indistinguishable from "there was no deployment".
    const view = executionPresentation(
      execution({ actions: [action({ receipt_ref: null, status: 'dispatched', resolved: false })] }),
      SERVER_TIME
    );

    expect(view.evidence[0].reference).toBeNull();
    expect(view.evidence[0].detail).toMatch(/pending/i);
    expect(view.evidence[0].resolved).toBe(false);
  });

  it('carries the server-supplied resolved flag rather than deriving it', () => {
    // The trap: `status !== 'prepared'` counts `unknown` as resolved, which is how
    // a green worker status hides an outstanding gate.
    const view = executionPresentation(
      execution({ actions: [action({ status: 'unknown', resolved: false })] }),
      SERVER_TIME
    );

    expect(view.evidence[0].resolved).toBe(false);
  });

  it('renders review, merge, deployment and evaluation receipts through one shape', () => {
    // This child owns the shared evidence presentation for its sibling acceptance
    // parents: later handlers populate `kind` and light this up rather than each
    // building a dashboard. A per-kind branch here becomes four divergent views.
    const kinds = ['review', 'merge', 'deployment', 'evaluation'];
    const view = executionPresentation(
      execution({
        actions: kinds.map((kind, index) =>
          action({ id: `action-${index}`, kind, receipt_ref: `${kind}/ref-${index}` })
        ),
      }),
      SERVER_TIME
    );

    expect(view.evidence.map((item) => item.kind)).toEqual(kinds);
    for (const item of view.evidence) {
      expect(item.reference).toMatch(new RegExp(`^${item.kind}/ref-`));
      expect(Object.keys(item).sort()).toEqual(
        ['detail', 'kind', 'label', 'reference', 'resolved'].sort()
      );
    }
  });

  it('says so when the action list is capped', () => {
    // A silently truncated list lets an operator conclude a step never happened.
    const view = executionPresentation(
      execution({ action_overflow: true, actions: [action()] }),
      SERVER_TIME
    );

    expect(view.truncationNote).toMatch(/not shown|not the whole history/i);
  });

  it('adds no truncation note when the list is complete', () => {
    expect(executionPresentation(execution(), SERVER_TIME).truncationNote).toBeUndefined();
  });
});

describe('time is computed from the server clock', () => {
  it('ages relative to server_time, not the browser clock', () => {
    // A browser clock may be wrong or in another zone, and it would be wrong on
    // exactly the field an operator acts on.
    expect(relativeAge('2026-09-18T09:00:00+00:00', SERVER_TIME)).toBe('3 hours ago');
    expect(relativeAge('2026-09-17T12:00:00+00:00', SERVER_TIME)).toBe('1 day ago');
    expect(relativeAge('2026-09-18T11:58:00+00:00', SERVER_TIME)).toBe('2 minutes ago');
  });

  it('reports a sub-minute age as "just now" rather than "0 minutes ago"', () => {
    expect(relativeAge('2026-09-18T11:59:30+00:00', SERVER_TIME)).toBe('just now');
  });

  it('treats a future progress stamp as just now rather than a negative age', () => {
    // Clock skew between server and database is real; "in -4 minutes" reads as a
    // bug in the data and distracts from whatever the operator came to do.
    expect(relativeAge('2026-09-18T12:04:00+00:00', SERVER_TIME)).toBe('just now');
  });

  it('counts down to the next check and says when one is already due', () => {
    expect(relativeDue('2026-09-18T12:15:00+00:00', SERVER_TIME)).toBe('in 15 minutes');
    // Past-due is stated. Hiding it would conceal an engine that has stopped
    // picking work up — which looks exactly like work quietly in progress.
    expect(relativeDue('2026-09-18T11:00:00+00:00', SERVER_TIME)).toBe('now due');
  });

  it('returns null rather than a wrong answer for absent or unparseable instants', () => {
    expect(relativeAge(null, SERVER_TIME)).toBeNull();
    expect(relativeDue(null, SERVER_TIME)).toBeNull();
    expect(relativeAge('not a date', SERVER_TIME)).toBeNull();
  });
});

describe('cycles', () => {
  const view: FlowExecution = {
    flow_id: 'flow-1',
    server_time: SERVER_TIME,
    executions: [
      execution({ id: 'exec-1', cycle: 1, status: 'superseded', revision: 9 }),
      execution({ id: 'exec-2', cycle: 2, phase: 'delivering' }),
    ],
    total: 2,
    limit: 200,
    offset: 0,
    legacy: false,
  };

  it('picks the newest cycle by cycle number, not array order', () => {
    // Showing an earlier cycle's block would send an operator to resolve something
    // the current cycle has already moved past.
    const reversed = { ...view, executions: [...view.executions].reverse() };

    expect(executionForNode(view, 'node-1').execution?.id).toBe('exec-2');
    expect(executionForNode(reversed, 'node-1').execution?.id).toBe('exec-2');
  });

  it('counts earlier cycles so a retry is not shown as the first attempt', () => {
    const { earlierCycles } = executionForNode(view, 'node-1');

    expect(earlierCycles).toBe(1);
    expect(executionPresentation(view.executions[1], SERVER_TIME, earlierCycles).cycleNote).toMatch(
      /Cycle 2.*1 earlier cycle/
    );
  });

  it('returns no execution for a node the ledger does not cover', () => {
    expect(executionForNode(view, 'node-absent').execution).toBeNull();
  });

  it('returns no execution when the view has not loaded', () => {
    expect(executionForNode(undefined, 'node-1').execution).toBeNull();
  });

  it('describes a superseded cycle as replaced, not as failed or complete', () => {
    const projected = executionPresentation(execution({ status: 'superseded' }), SERVER_TIME);

    expect(projected.headline).toMatch(/replaced/i);
    expect(projected.tone).toBe('unknown');
  });
});

describe('phases', () => {
  it('labels every phase in the vocabulary', () => {
    const phases = [
      'admitted',
      'preparing',
      'delivering',
      'submitting',
      'awaiting_review',
      'repairing',
      'settling',
      'concluded',
    ] as const;

    for (const phase of phases) {
      // A hole in the mapping table renders as a *different* stage of delivery,
      // which is exactly the misreport this feature exists to remove.
      expect(phaseLabel(phase)).toBeTruthy();
      expect(phaseLabel(phase)).not.toMatch(/_/);
    }
    expect(new Set(phases.map(phaseLabel)).size).toBe(phases.length);
  });

  it('shows an unrecognised phase verbatim rather than mapping it to a neighbour', () => {
    // A wrong-but-plausible stage name is worse than an unfamiliar one.
    expect(phaseLabel('teleporting' as never)).toBe('teleporting');
  });

  it('describes awaiting_external as waiting on something else, not as stalled', () => {
    const view = executionPresentation(
      execution({ status: 'awaiting_external', phase: 'awaiting_review' }),
      SERVER_TIME
    );

    expect(view.tone).toBe('pending');
    expect(view.headline).toMatch(/waiting on an external system/i);
  });
});
