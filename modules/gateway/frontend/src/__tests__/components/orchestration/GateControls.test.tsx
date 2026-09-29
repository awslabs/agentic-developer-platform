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
 * `reason` and the reviewed revision hash; `tests/orchestration/test_controls.py` covers the server side.
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
  getGatePlanPreview: vi.fn(),
}));

const mockHasPermission = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ hasPermission: mockHasPermission }),
}));

const mockUseFeatures = vi.fn();
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockUseFeatures(),
}));

import { approveGate, rejectGate, resumeNode, getGatePlanPreview } from '@/services/orchestration';

const hash = 'a'.repeat(64);
const plan = { version: 1, plan_hash: hash, superseded_at: null, plan_document: { title: 'Repair plan', nodes: [{address: 'flow/epic-1/wave-1/gate-a', title: 'Design gate', kind: 'gate'}], proposed_execution_policy: {allowed_actions: ['review', 'merge']} } };
const mockPreview = vi.mocked(getGatePlanPreview);
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
  vi.resetAllMocks();
  mockPreview.mockResolvedValue(plan);
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
    await waitFor(() => expect(screen.getByTestId('gate-approve')).toBeEnabled());
    await user.click(screen.getByRole('button', { name: 'Accept evaluation' }));
    expect(mockApprove).toHaveBeenCalledWith('node-1', undefined, hash, undefined);
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
    await waitFor(() => expect(screen.getByTestId('gate-approve')).toBeEnabled());
    await user.click(screen.getByTestId('gate-approve'));

    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith('node-1', 'looks right', hash, undefined));
  });

  it('sends no actor_kind — attribution is the session\'s to decide, not the caller\'s', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate'));

    await waitFor(() => expect(screen.getByTestId('gate-approve')).toBeEnabled());
    await user.click(screen.getByTestId('gate-approve'));

    await waitFor(() => expect(mockApprove).toHaveBeenCalled());
    // The service takes id, reason, revision hash and the execution preview. There is no argument position in which
    // this component could assert who the actor is.
    expect(mockApprove.mock.calls[0]).toHaveLength(4);
    expect(mockApprove.mock.calls[0][1]).toBeUndefined();
  });

  it('rejecting calls the reject endpoint, not approve', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate'));

    expect(screen.getByTestId('gate-reject')).toBeDisabled();
    await user.type(screen.getByTestId('gate-controls-reason'), 'Clarify the migration plan');
    await user.click(screen.getByTestId('gate-reject'));

    await waitFor(() => expect(mockReject).toHaveBeenCalledWith('node-1', 'Clarify the migration plan', hash));
    expect(screen.getByRole('status')).toHaveTextContent('No revision agent has been started');
    expect(mockApprove).not.toHaveBeenCalled();
  });

  it('an empty reason is sent as absent rather than as an empty string', async () => {
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate'));

    await user.type(screen.getByTestId('gate-controls-reason'), '   ');
    await waitFor(() => expect(screen.getByTestId('gate-approve')).toBeEnabled());
    await user.click(screen.getByTestId('gate-approve'));

    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith('node-1', undefined, hash, undefined));
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
    await waitFor(() => expect(screen.getByTestId('gate-approve')).toBeEnabled());
    await user.click(screen.getByTestId('gate-approve'));

    await waitFor(() => expect(input).toHaveValue(''));
  });

  it('disables the buttons while a decision is in flight, so one click is one decision', async () => {
    const user = userEvent.setup();
    let release: (value: unknown) => void = () => {};
    mockApprove.mockReturnValue(new Promise((resolve) => { release = resolve; }));

    renderControls(makeNode('awaiting_gate'));
    await waitFor(() => expect(screen.getByTestId('gate-approve')).toBeEnabled());
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
    await waitFor(() => expect(screen.getByTestId('gate-approve')).toBeEnabled());
    await user.click(screen.getByTestId('gate-approve'));

    expect(await screen.findByText(/already answered/i)).toBeInTheDocument();
  });

  it('reports a refusal with no message without rendering an empty alert', async () => {
    const user = userEvent.setup();
    mockApprove.mockRejectedValue(new Error(''));

    renderControls(makeNode('awaiting_gate'));
    await waitFor(() => expect(screen.getByTestId('gate-approve')).toBeEnabled());
    await user.click(screen.getByTestId('gate-approve'));

    expect(await screen.findByText(/decision was refused/i)).toBeInTheDocument();
  });
});


describe('reviewed revision binding', () => {
  it('blocks approval while the plan is loading', () => {
    mockPreview.mockReturnValue(new Promise(() => {}));
    renderControls(makeNode('awaiting_gate'));
    expect(screen.getByTestId('gate-approve')).toBeDisabled();
    expect(mockApprove).not.toHaveBeenCalled();
  });
  it('blocks approval when preview cannot be loaded', async () => {
    mockPreview.mockRejectedValue({detail: 'Plan read unavailable'});
    renderControls(makeNode('awaiting_gate'));
    expect(await screen.findByText('Plan read unavailable')).toBeInTheDocument();
    expect(screen.getByTestId('gate-approve')).toBeDisabled();
  });
  it('blocks a gate absent from the displayed revision', async () => {
    mockPreview.mockResolvedValue({...plan, plan_document: {...plan.plan_document, nodes: []}});
    renderControls(makeNode('awaiting_gate'));
    expect(await screen.findByText(/gate is absent/)).toBeInTheDocument();
    expect(screen.getByTestId('gate-approve')).toBeDisabled();
  });
  it.each(['Plan changed; review again', {message: 'Plan changed; review again'}])('shows server detail and requires an explicit decision after reloading', async detail => {
    const user = userEvent.setup();
    mockApprove.mockRejectedValue({detail});
    renderControls(makeNode('awaiting_gate'));
    expect(await screen.findByText(/Proposed execution authority/)).toBeInTheDocument();
    await user.click(screen.getByTestId('gate-approve'));
    expect(await screen.findByText('Plan changed; review again')).toBeInTheDocument();
    expect(mockApprove).toHaveBeenCalledWith('node-1', undefined, hash, undefined);
    mockPreview.mockResolvedValue({...plan, version: 2, plan_hash: 'b'.repeat(64)});
    await user.click(screen.getByText('Reload plan preview'));
    expect(await screen.findByText(/Review plan version 2/)).toBeInTheDocument();
    expect(mockApprove).toHaveBeenCalledTimes(1);
    await user.click(screen.getByTestId('gate-approve'));
    expect(mockApprove).toHaveBeenLastCalledWith('node-1', undefined, 'b'.repeat(64), undefined);
  });
});

describe('executable next step', () => {
  it('explains missing configuration and keeps approval disabled', async () => {
    mockPreview.mockResolvedValue({ ...plan, execution: { required: true, ready: false, runs: [], problems: ['E32 needs a workflow binding'] } });
    renderControls(makeNode('awaiting_gate'));
    expect(await screen.findByText('E32 needs a workflow binding')).toBeInTheDocument();
    expect(screen.getByTestId('gate-approve')).toBeDisabled();
    expect(mockApprove).not.toHaveBeenCalled();
  });

  it('shows the target and sends the reviewed execution with one approval', async () => {
    const execution = { required: true, ready: true, snapshot: 'b'.repeat(64), problems: [], runs: [{ node_id: 'eval-1', title: 'E32', workflow: 'eval-cli-uplift.yml', target: { account_id: '123456789012', region: 'us-east-1', resource_id: 'dev' }, acceptance: 'human', criteria: ['cleanup', 'E32'] }] };
    mockPreview.mockResolvedValue({ ...plan, execution });
    const user = userEvent.setup();
    renderControls(makeNode('awaiting_gate'));
    expect(await screen.findByText(/Account 123456789012/)).toBeInTheDocument();
    await user.click(screen.getByTestId('gate-approve'));
    expect(mockApprove).toHaveBeenCalledWith('node-1', undefined, hash, execution);
  });
});
