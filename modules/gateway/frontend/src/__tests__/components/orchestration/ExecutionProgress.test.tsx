/**
 * Rendering tests for the delivery execution panel — issue #5145.
 *
 * The projection is tested directly in `utils/executionProgress.test.ts`. What is
 * asserted here is what actually reaches an operator's screen, because a correct
 * projection can still be rendered misleadingly: a block whose owner never appears,
 * an absence that renders as nothing at all, a truncated list with no note, a
 * receipt turned into a clickable link.
 *
 * Fixtures are shaped exactly like the API response — the same field set the route
 * serves in `FlowExecutionResponse` — so the contract cannot drift silently between
 * the two. A fixture that invents a field the server never sends is a test that
 * proves nothing.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ExecutionProgress } from '@/components/orchestration/ExecutionProgress';
import type { ExecutionAction, ExecutionSummary } from '@/types/orchestration';

const SERVER_TIME = '2026-09-18T12:00:00+00:00';
const NODE_REF = 'story-a';

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

function renderPanel(props: Partial<React.ComponentProps<typeof ExecutionProgress>> = {}) {
  return render(
    <ExecutionProgress
      nodeRef={NODE_REF}
      execution={execution()}
      serverTime={SERVER_TIME}
      legacy={false}
      {...props}
    />
  );
}

describe('every phase renders', () => {
  const cases: [ExecutionSummary['phase'], ExecutionSummary['status'], RegExp][] = [
    ['admitted', 'runnable', /accepted for delivery/i],
    ['preparing', 'runnable', /preparing/i],
    ['delivering', 'runnable', /delivering/i],
    ['submitting', 'awaiting_external', /submitting/i],
    ['awaiting_review', 'awaiting_external', /awaiting review/i],
    ['repairing', 'runnable', /repairing/i],
    ['settling', 'runnable', /settling/i],
    ['concluded', 'concluded', /concluded/i],
  ];

  for (const [phase, status, expected] of cases) {
    it(`renders ${phase}/${status} as itself`, () => {
      renderPanel({
        execution: execution({
          phase,
          status,
          next_check_at: status === 'concluded' ? null : '2026-09-18T12:15:00+00:00',
        }),
      });

      expect(screen.getByTestId(`execution-headline-${NODE_REF}`).textContent).toMatch(expected);
      // The phase and status are exposed on the container too, so an assertion
      // about them does not depend on prose wording that may be reworded later.
      const panel = screen.getByTestId(`execution-progress-${NODE_REF}`);
      expect(panel).toHaveAttribute('data-phase', phase);
      expect(panel).toHaveAttribute('data-status', status);
    });
  }
});

describe('a typed block is routable on screen', () => {
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

  it('shows the owner and the required input', () => {
    // A block an operator can read but not act on has not solved the problem this
    // feature exists for — the logs they would otherwise read expire.
    renderPanel({ execution: blocked });

    const block = screen.getByTestId(`execution-block-${NODE_REF}`);
    expect(block.textContent).toMatch(/A platform operator/);
    expect(block.textContent).toMatch(/approve the wave gate for story-a/);
  });

  it('lists the outstanding approvals', () => {
    renderPanel({ execution: blocked });

    const gates = screen.getByTestId(`execution-gates-${NODE_REF}`);
    expect(gates.textContent).toMatch(/gate:security-review/);
    expect(gates.textContent).toMatch(/gate:cost-approval/);
  });

  it('presents blocked as needing attention, not as an error', () => {
    renderPanel({ execution: blocked });

    expect(screen.getByTestId(`execution-progress-${NODE_REF}`)).toHaveAttribute(
      'data-tone',
      'attention'
    );
    expect(screen.getByTestId(`execution-headline-${NODE_REF}`).textContent).not.toMatch(/fail/i);
  });

  it('shows how long it has actually been stuck', () => {
    // From the block's own `progressed_at`, which the ledger does not reset when a
    // row blocks — so this is real elapsed time, not the age of the block write.
    renderPanel({ execution: blocked });

    expect(screen.getByTestId(`execution-progressed-${NODE_REF}`).textContent).toMatch(
      /3 hours ago/
    );
  });

  it('shows no block section when the execution is not blocked', () => {
    renderPanel();

    expect(screen.queryByTestId(`execution-block-${NODE_REF}`)).toBeNull();
    expect(screen.queryByTestId(`execution-gates-${NODE_REF}`)).toBeNull();
  });
});

describe('the next check time is visible', () => {
  it('shows when the engine will next look', () => {
    // Without it, "waiting" is indistinguishable from "abandoned".
    renderPanel();

    expect(screen.getByTestId(`execution-next-check-${NODE_REF}`).textContent).toMatch(
      /in 15 minutes/
    );
  });

  it('says a past-due check is due now rather than hiding it', () => {
    // A check the engine has missed is the signal that pickup has stopped — which
    // otherwise looks exactly like work quietly in progress.
    renderPanel({ execution: execution({ next_check_at: '2026-09-18T11:00:00+00:00' }) });

    expect(screen.getByTestId(`execution-next-check-${NODE_REF}`).textContent).toMatch(/now due/);
  });

  it('omits the next check for a terminal execution', () => {
    // A wake-up time on a concluded cycle would imply work still to come.
    renderPanel({
      execution: execution({ status: 'concluded', phase: 'concluded', next_check_at: null }),
    });

    expect(screen.queryByTestId(`execution-next-check-${NODE_REF}`)).toBeNull();
  });
});

describe('evidence', () => {
  it('reads a missing receipt as pending, not as nothing having happened', () => {
    renderPanel({
      execution: execution({
        actions: [action({ receipt_ref: null, status: 'dispatched', resolved: false })],
      }),
    });

    const evidence = screen.getByTestId(`execution-evidence-${NODE_REF}`);
    expect(evidence.textContent).toMatch(/receipt pending/i);
  });

  it('shows an unknown outcome as needing checking, not as success or failure', () => {
    renderPanel({
      execution: execution({
        actions: [action({ status: 'unknown', resolved: false, receipt_ref: null })],
      }),
    });

    const evidence = screen.getByTestId(`execution-evidence-${NODE_REF}`);
    expect(evidence.textContent).toMatch(/could not be determined/i);
    expect(evidence.querySelector('[data-resolved="false"]')).not.toBeNull();
  });

  it('renders all four receipt kinds through one generic display', () => {
    // The shared contract this child owns for its sibling acceptance parents.
    renderPanel({
      execution: execution({
        actions: ['review', 'merge', 'deployment', 'evaluation'].map((kind, index) =>
          action({ id: `action-${index}`, kind, receipt_ref: `${kind}/ref-${index}` })
        ),
      }),
    });

    const evidence = screen.getByTestId(`execution-evidence-${NODE_REF}`);
    for (const kind of ['review', 'merge', 'deployment', 'evaluation']) {
      expect(evidence.querySelector(`[data-kind="${kind}"]`)).not.toBeNull();
    }
  });

  it('renders a receipt reference as text, never as a link', () => {
    // These are provider identifiers, not URLs. Building an href from one would
    // guess at a host — and a guessed link is worse than a copyable id. It is also
    // the second line of defence behind the server's fail-closed sanitiser.
    renderPanel({ execution: execution({ actions: [action()] }) });

    const evidence = screen.getByTestId(`execution-evidence-${NODE_REF}`);
    expect(evidence.textContent).toMatch(/pr\/PR_kwDOABCD1234/);
    expect(evidence.querySelectorAll('a')).toHaveLength(0);
  });

  it('says so when older steps are not shown', () => {
    renderPanel({ execution: execution({ action_overflow: true, actions: [action()] }) });

    expect(screen.getByTestId(`execution-truncated-${NODE_REF}`).textContent).toMatch(
      /not shown|not the whole history/i
    );
  });
});

describe('absence is stated, never implied to be success', () => {
  it('names a legacy flow as having no execution record', () => {
    renderPanel({ execution: null, legacy: true });

    const absent = screen.getByTestId(`execution-absent-${NODE_REF}`);
    expect(absent.textContent).toMatch(/no execution record/i);
    expect(absent.textContent).not.toMatch(/complete|success|delivered/i);
    expect(absent).toHaveAttribute('data-legacy', 'true');
  });

  it('distinguishes a node that has not started from a flow with no ledger', () => {
    renderPanel({ execution: null, legacy: false });

    const absent = screen.getByTestId(`execution-absent-${NODE_REF}`);
    expect(absent.textContent).toMatch(/no delivery activity recorded yet/i);
    expect(absent).toHaveAttribute('data-legacy', 'false');
  });

  it('renders something rather than nothing when there is no record', () => {
    // Rendering nothing lets the story journey's stage list stand alone and read as
    // a complete account of delivery.
    const { container } = renderPanel({ execution: null, legacy: true });

    expect(container.textContent?.trim()).not.toBe('');
  });
});

describe('cycles and revisions', () => {
  it('notes earlier cycles so a retry is not presented as the first attempt', () => {
    renderPanel({ execution: execution({ cycle: 2 }), earlierCycles: 1 });

    expect(screen.getByTestId(`execution-cycle-${NODE_REF}`).textContent).toMatch(
      /Cycle 2.*1 earlier cycle/
    );
  });

  it('exposes the rendered revision so a stale overwrite is observable', () => {
    // The hook guards against a stale poll replacing newer data; surfacing the
    // revision here makes that regression visible in the DOM rather than only in
    // the hook's own unit test.
    renderPanel({ execution: execution({ revision: 12 }) });

    expect(screen.getByTestId(`execution-progress-${NODE_REF}`)).toHaveAttribute(
      'data-revision',
      '12'
    );
  });

  it('describes a superseded cycle as replaced', () => {
    renderPanel({ execution: execution({ status: 'superseded', next_check_at: null }) });

    expect(screen.getByTestId(`execution-headline-${NODE_REF}`).textContent).toMatch(/replaced/i);
  });
});

describe('read-only surface', () => {
  it('renders no buttons or form controls', () => {
    // The issue's scope boundary: approval and recovery stay with the existing
    // controls, and the endpoint behind this panel has no mutating verb. A button
    // here would be the first step toward a second, divergent control surface.
    const { container } = renderPanel({
      execution: execution({
        status: 'blocked',
        block: {
          code: 'human_gate_required',
          owner: 'platform-operator',
          required_input: 'approve the wave gate',
          remaining_gates: ['gate:merge-approval'],
          progressed_at: '2026-09-18T09:00:00+00:00',
          detail: null,
        },
        actions: [action()],
      }),
    });

    expect(container.querySelectorAll('button')).toHaveLength(0);
    expect(container.querySelectorAll('input, select, textarea, form')).toHaveLength(0);
  });

  it('publishes no part of the authority binding', () => {
    // The route deliberately serves neither `accepted_plan_version` (an acceptance
    // record, which the router guard requires approval authority to read) nor the
    // claim pair. A panel that invented them from somewhere else would reintroduce
    // exactly what the backend excluded, on a surface with no such permission.
    const { container } = renderPanel({
      execution: execution({ actions: [action()] }),
    });

    expect(container.textContent).not.toMatch(/accepted plan|plan version|claim/i);
  });
});

describe('evaluation decision evidence', () => {
  it.each([true, false])('renders criterion results without clearing a human gate (pass=%s)', (passed) => {
    renderPanel({
      nodeAccepted: false,
      execution: execution({
        phase: 'evaluation_pending', status: 'blocked',
        block: { code: 'human_gate_required', owner: 'platform-operator', required_input: 'Review the security gate', remaining_gates: ['security'], progressed_at: null, detail: null },
        actions: [action({ kind: 'evaluation_evidence', evidence_summary: {
          actual_revision: 'a'.repeat(40), harness_revision: 'b'.repeat(40),
          completed_at: SERVER_TIME, expires_at: SERVER_TIME, mandatory_passed: passed,
          criteria: [{ criterion_id: 'API-1', outcome: passed ? 'pass' : 'fail' }],
        } })],
      }),
    });
    expect(screen.getByLabelText('Evaluation criteria')).toHaveTextContent(`API-1: ${passed ? 'pass' : 'fail'}`);
    expect(screen.getByLabelText('Evaluation criteria')).toHaveTextContent('a'.repeat(40));
    expect(screen.getByLabelText('Evaluation criteria')).toHaveTextContent('b'.repeat(40));
    expect(screen.getByTestId(`execution-gates-${NODE_REF}`)).toHaveTextContent('security');
    expect(screen.getByTestId(`execution-progress-${NODE_REF}`)).toHaveAttribute('data-tone', 'attention');
  });
});
