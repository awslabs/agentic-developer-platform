/**
 * Tests for the gate-decision and resume controls — issue #4213.
 *
 * The security-relevant assertions here are the *negative* ones. A test that only
 * proves "Approve fires a POST" would pass identically against a component that
 * renders Approve for every caller, which is the failure mode that matters: the
 * control is meant to be absent without authority, absent when the engine is off,
 * and incapable of asserting who the actor is.
 *
 * `actor_kind` attribution is deliberately NOT asserted as a body field here,
 * because the whole point is that this component never sends one — the backend
 * derives it from the session. The assertion is that the request body contains
 * only `reason`; `tests/orchestration/test_controls.py` covers the server side.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { GateControls } from '@/components/orchestration/GateControls';
import { Permission } from '@/types';
import type { GraphNode, NodeEngineState, NodeKind } from '@/types/orchestration';

vi.mock('@/services/orchestration', () => ({
  approveGate: vi.fn(),
  rejectGate: vi.fn(),
  resumeNode: vi.fn(),
}));

const mockHasPermission = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ hasPermission: mockHasPermission }),
}));

const mockUseFeatures = vi.fn();
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockUseFeatures(),
}));

import { approveGate, rejectGate, resumeNode } from '@/services/orchestration';

const mockApprove = approveGate as ReturnType<typeof vi.fn>;
const mockReject = rejectGate as ReturnType<typeof vi.fn>;
const mockResume = resumeNode as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeNode(state: NodeEngineState, kind: NodeKind = 'gate'): GraphNode {
  return {
    id: 'node-1',
    epic_ref: 'epic-1',
    wave_ref: 'wave-1',
    node_ref: 'gate-a',
    kind,
    title: 'Design gate',
    state,
    stalled: false,
    issue_ref: null,
    attempts: 0,
    cost: { status: 'unknown', amount_usd: null, reason: 'not_started', scope: 'agent run costs only; excludes build/infra' },
  };
}

function renderControls(node: GraphNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <GateControls node={node} flowId="flow-1" />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockHasPermission.mockReturnValue(true);
  mockUseFeatures.mockReturnValue({ orchestration_engine: true });
  mockApprove.mockResolvedValue({
    node_id: 'node-1',
    status: 'applied',
    state: 'passed',
    decision_id: 'dec-1',
    actor_kind: 'human',
    message: 'gate approved',
  });
  mockReject.mockResolvedValue({
    node_id: 'node-1',
    status: 'applied',
    state: 'rejected_at_gate',
    decision_id: 'dec-2',
    actor_kind: 'human',
    message: 'changes requested',
  });
  mockResume.mockResolvedValue({
    node_id: 'node-1',
    from_state: 'halted',
    state: 'ready',
    decision_id: 'dec-3',
    actor_kind: 'human',
  });
});

// ---------------------------------------------------------------------------

describe('authority (AC-5/AC-6): the control is absent, not disabled', () => {
  it('renders nothing without the approval permission', () => {
    mockHasPermission.mockReturnValue(false);

    renderControls(makeNode('awaiting_gate'));

    expect(screen.queryByTestId('gate-approve')).not.toBeInTheDocument();
    expect(screen.queryByTestId('gate-reject')).not.toBeInTheDocument();
  });

  it('gates on the approval permission specifically, not merely on being logged in', () => {
    renderControls(makeNode('awaiting_gate'));

    expect(mockHasPermission).toHaveBeenCalledWith(Permission.PLAN_APPROVE);
  });

  it('shows the controls when the permission is held', () => {
    renderControls(makeNode('awaiting_gate'));

    expect(screen.getByTestId('gate-approve')).toBeInTheDocument();
    expect(screen.getByTestId('gate-reject')).toBeInTheDocument();
  });

  it('hides the control rather than disabling it, so no unusable button is advertised', () => {
    mockHasPermission.mockReturnValue(false);

    const { container } = renderControls(makeNode('awaiting_gate'));

    // Nothing at all, as opposed to a present-but-disabled button.
    expect(container.querySelectorAll('button')).toHaveLength(0);
  });
});

describe('the fail-closed engine flag', () => {
  it('renders nothing when orchestration_engine is off, even with the permission', () => {
    mockUseFeatures.mockReturnValue({ orchestration_engine: false });

    renderControls(makeNode('awaiting_gate'));

    expect(screen.queryByTestId('gate-approve')).not.toBeInTheDocument();
  });
});

describe('which control a state earns', () => {
  it('offers evidence acceptance on a finished evaluation', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate', 'eval'));
    await user.click(screen.getByRole('button', { name: 'Accept evaluation' }));
    expect(mockApprove).toHaveBeenCalledWith('node-1', undefined);
  });

  it('reopens a gate after requested changes', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('rejected_at_gate'));
    expect(screen.getByText(/Reopening does not approve or start the work/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Reopen review' }));
    expect(mockResume).toHaveBeenCalledWith('node-1', undefined);
  });

  it('allows an explicit retry, but no approval, while merge checks are pending', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_merge', 'story'));
    expect(screen.queryByTestId('gate-approve')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry story' }));
    expect(mockResume).toHaveBeenCalledWith('node-1', undefined);
  });

  it('offers approve/reject on a gate awaiting a decision', () => {
    renderControls(makeNode('awaiting_gate'));

    expect(screen.getByTestId('gate-approve')).toBeInTheDocument();
    expect(screen.queryByTestId('node-resume')).not.toBeInTheDocument();
  });

  it('offers resume on a failed node — a stall lands here and is recoverable', () => {
    renderControls(makeNode('failed', 'story'));

    expect(screen.getByTestId('node-resume')).toBeInTheDocument();
    expect(screen.queryByTestId('gate-approve')).not.toBeInTheDocument();
  });

  it('names a halt override for what it is', () => {
    renderControls(makeNode('halted', 'story'));

    expect(screen.getByTestId('node-resume')).toHaveTextContent(/override halt/i);
  });

  it('offers nothing on a running node — every other edge belongs to the engine', () => {
    renderControls(makeNode('running', 'story'));

    expect(screen.queryByTestId('gate-approve')).not.toBeInTheDocument();
    expect(screen.queryByTestId('node-resume')).not.toBeInTheDocument();
  });

  it('offers nothing on an already-passed gate, so a gate cannot be answered twice from the UI', () => {
    renderControls(makeNode('passed'));

    expect(screen.queryByTestId('gate-approve')).not.toBeInTheDocument();
  });

  it('does not offer a gate decision on a story that is somehow awaiting_gate', () => {
    renderControls(makeNode('awaiting_gate', 'story'));

    expect(screen.queryByTestId('gate-approve')).not.toBeInTheDocument();
  });
});

describe('recording a decision', () => {
  it('approving posts the node id and reflects the new state', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate'));

    await user.type(screen.getByTestId('gate-controls-reason'), 'looks right');
    await user.click(screen.getByTestId('gate-approve'));

    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith('node-1', 'looks right'));
  });

  it('sends no actor_kind — attribution is the session\'s to decide, not the caller\'s', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate'));

    await user.click(screen.getByTestId('gate-approve'));

    await waitFor(() => expect(mockApprove).toHaveBeenCalled());
    // The service takes (id, reason) only. There is no argument position in which
    // this component could assert who the actor is.
    expect(mockApprove.mock.calls[0]).toHaveLength(2);
    expect(mockApprove.mock.calls[0][1]).toBeUndefined();
  });

  it('rejecting calls the reject endpoint, not approve', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate'));

    expect(screen.getByTestId('gate-reject')).toBeDisabled();
    await user.type(screen.getByTestId('gate-controls-reason'), 'Clarify the migration plan');
    await user.click(screen.getByTestId('gate-reject'));

    await waitFor(() => expect(mockReject).toHaveBeenCalledWith('node-1', 'Clarify the migration plan'));
    expect(screen.getByRole('status')).toHaveTextContent('No revision agent has been started');
    expect(mockApprove).not.toHaveBeenCalled();
  });

  it('an empty reason is sent as absent rather than as an empty string', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate'));

    await user.type(screen.getByTestId('gate-controls-reason'), '   ');
    await user.click(screen.getByTestId('gate-approve'));

    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith('node-1', undefined));
  });

  it('resuming posts to the resume endpoint', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('halted', 'story'));

    await user.type(screen.getByTestId('gate-controls-reason'), 'root cause fixed');
    await user.click(screen.getByTestId('node-resume'));

    await waitFor(() => expect(mockResume).toHaveBeenCalledWith('node-1', 'root cause fixed'));
  });

  it('clears the reason after a recorded decision so it cannot be reused by accident', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate'));

    const input = screen.getByTestId('gate-controls-reason');
    await user.type(input, 'one-off justification');
    await user.click(screen.getByTestId('gate-approve'));

    await waitFor(() => expect(input).toHaveValue(''));
  });

  it('disables the buttons while a decision is in flight, so one click is one decision', async () => {
    const user = userEvent.setup();
    let release: (value: unknown) => void = () => {};
    mockApprove.mockReturnValue(new Promise((resolve) => { release = resolve; }));

    renderControls(makeNode('awaiting_gate'));
    await user.click(screen.getByTestId('gate-approve'));

    await waitFor(() => expect(screen.getByTestId('gate-approve')).toBeDisabled());
    expect(screen.getByTestId('gate-reject')).toBeDisabled();

    release({ node_id: 'node-1', status: 'applied', state: 'passed', decision_id: 'd', actor_kind: 'human', message: 'ok' });
  });
});

describe('a refused decision', () => {
  it('surfaces the refusal instead of implying the decision landed', async () => {
    const user = userEvent.setup();
    mockApprove.mockRejectedValue(new Error('this gate was already answered'));

    renderControls(makeNode('awaiting_gate'));
    await user.click(screen.getByTestId('gate-approve'));

    expect(await screen.findByText(/already answered/i)).toBeInTheDocument();
  });

  it('reports a refusal with no message without rendering an empty alert', async () => {
    const user = userEvent.setup();
    mockApprove.mockRejectedValue(new Error(''));

    renderControls(makeNode('awaiting_gate'));
    await user.click(screen.getByTestId('gate-approve'));

    expect(await screen.findByText(/decision was refused/i)).toBeInTheDocument();
  });
});
