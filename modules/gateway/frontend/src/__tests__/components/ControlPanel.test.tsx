/**
 * Tests for ControlPanel — live run controls — Issue #3966.
 *
 * The tests are organised around the ways this panel could mislead an operator,
 * because those are the failures that matter: showing a control that cannot
 * work, showing one run's state under another run's ID, claiming a pause took
 * effect before it did, or reporting a lost command as delivered.
 *
 * Behaviour is asserted through the rendered DOM and the service calls actually
 * made. Asserting that a query options object "contains refetchInterval" would
 * pass against a poll that never fires, so the polling tests advance timers and
 * count real calls instead (the convention established in AgentActivity.test.tsx).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { FeatureFlags } from '@/services/features';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('@/services/agentControl', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/agentControl')>('@/services/agentControl');
  return {
    ...actual,
    getControlState: vi.fn(),
    sendControlCommand: vi.fn(),
    sendSteerCommand: vi.fn(),
  };
});

const mockFeatures: FeatureFlags = {
  chat: true,
  knowledge: true,
  indexing: true,
  connections: true,
  credentials: true,
  system_dashboard: true,
  logs: true,
  gitlab: false,
  orchestration_engine: false,
  budget_spend: false,
  agent_control: true,
  new_ui: false,
  superplane: false,
  agent_models: false,
};

/** Mutable feature-query result, so each test can set flag/pending/error. */
const featuresState: { data: FeatureFlags | undefined; isPending: boolean; isError: boolean } = {
  data: mockFeatures,
  isPending: false,
  isError: false,
};

vi.mock('@/hooks/useFeatures', () => ({
  useFeaturesQuery: () => featuresState,
  useFeatures: () => featuresState.data ?? mockFeatures,
}));

import { ControlPanel, CONTROL_POLL_MS } from '@/components/ControlPanel';
import { getControlState, sendControlCommand, sendSteerCommand } from '@/services/agentControl';
import type { ControlStateResponse } from '@/services/agentControl';

const mockGetState = getControlState as ReturnType<typeof vi.fn>;
const mockSendCommand = sendControlCommand as ReturnType<typeof vi.fn>;
const mockSendSteer = sendSteerCommand as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A controllable run with every verb available — the permissive baseline. */
function controllableState(overrides: Partial<ControlStateResponse> = {}): ControlStateResponse {
  return {
    run_id: 'run-1',
    generation: 1,
    available: true,
    reason: null,
    capabilities: { pause: true, resume: true, steer: true, abort: true },
    state: 'running',
    active_tool_count: 0,
    updated_at: '2026-09-24T10:00:00Z',
    commands: [],
    ...overrides,
  };
}

function renderPanel(props: Partial<React.ComponentProps<typeof ControlPanel>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ControlPanel invocationId="run-1" isOpen {...props} />
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}

beforeEach(() => {
  vi.clearAllMocks();
  featuresState.data = mockFeatures;
  featuresState.isPending = false;
  featuresState.isError = false;
  mockGetState.mockResolvedValue(controllableState());
  mockSendCommand.mockResolvedValue({
    run_id: 'run-1',
    action: 'pause',
    state: 'pause_requested',
    command_id: 'cmd-1',
    command_status: 'pending',
  });
  mockSendSteer.mockResolvedValue({
    run_id: 'run-1',
    action: 'steer',
    state: 'running',
    command_id: 'cmd-steer',
    command_status: 'pending',
  });
  // Default to a visible document.
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => 'visible',
  });
});

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// Fail-closed gating
// ---------------------------------------------------------------------------

describe('ControlPanel — fail-closed gating', () => {
  it('renders nothing when the feature flag is off', async () => {
    featuresState.data = { ...mockFeatures, agent_control: false };
    renderPanel();
    expect(screen.queryByRole('region', { name: /live run controls/i })).not.toBeInTheDocument();
    // And it must not even ask: a request per two seconds for a feature that is
    // switched off is both pointless and a signal in the access logs.
    expect(mockGetState).not.toHaveBeenCalled();
  });

  it('renders nothing while the feature flags are still loading', () => {
    // The fail-open this prevents: controls visible on every cold page load,
    // before the flags that would have hidden them arrive.
    featuresState.isPending = true;
    featuresState.data = undefined;
    renderPanel();
    expect(screen.queryByRole('region', { name: /live run controls/i })).not.toBeInTheDocument();
    expect(mockGetState).not.toHaveBeenCalled();
  });

  it('renders nothing when the feature-flag fetch failed', () => {
    // An outage is exactly when a control cannot work, so an error must not
    // leave the previous "on" answer showing.
    featuresState.isError = true;
    featuresState.data = undefined;
    renderPanel();
    expect(screen.queryByRole('region', { name: /live run controls/i })).not.toBeInTheDocument();
    expect(mockGetState).not.toHaveBeenCalled();
  });

  it('renders nothing when the containing modal is closed', () => {
    renderPanel({ isOpen: false });
    expect(screen.queryByRole('region', { name: /live run controls/i })).not.toBeInTheDocument();
    expect(mockGetState).not.toHaveBeenCalled();
  });

  it('offers no controls and does not poll for a run already in a terminal status', async () => {
    renderPanel({ isTerminalRun: true });
    expect(mockGetState).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
  });

  it('offers no controls when the run reports no live control channel', async () => {
    mockGetState.mockResolvedValue(
      controllableState({
        available: false,
        state: 'unavailable',
        capabilities: { pause: false, resume: false, steer: false, abort: false },
        reason: 'run has no registered control listener',
      }),
    );
    renderPanel();
    await waitFor(() => {
      expect(screen.getByTestId('control-unavailable')).toBeInTheDocument();
    });
    // The reason is shown, because "why not" is what the operator needs.
    expect(screen.getByTestId('control-unavailable')).toHaveTextContent(
      /no registered control listener/i,
    );
    expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
  });

  it.each([
    ['pause', /^pause$/i],
    ['resume', /^resume$/i],
    ['abort', /^abort$/i],
  ])('hides the %s control when the deployment lacks that capability', async (verb, pattern) => {
    mockGetState.mockResolvedValue(
      controllableState({
        capabilities: {
          pause: verb !== 'pause',
          resume: verb !== 'resume',
          steer: false,
          abort: verb !== 'abort',
        },
      }),
    );
    renderPanel();
    await waitFor(() => expect(screen.getByTestId('control-phase')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: pattern as RegExp })).not.toBeInTheDocument();
  });

  it('hides the steer box while the backend reports steer unsupported', async () => {
    // This is the live situation until S6 lands: the gateway does not route
    // steer, so its capability is false and no steer UI may appear.
    mockGetState.mockResolvedValue(
      controllableState({
        capabilities: { pause: true, resume: true, steer: false, abort: true },
      }),
    );
    renderPanel();
    await waitFor(() => expect(screen.getByTestId('control-phase')).toBeInTheDocument());
    expect(screen.queryByLabelText(/send an instruction/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument();
  });

  it('says so plainly when the run is reachable but offers no verbs at all', async () => {
    mockGetState.mockResolvedValue(
      controllableState({
        capabilities: { pause: false, resume: false, steer: false, abort: false },
      }),
    );
    renderPanel();
    await waitFor(() => {
      expect(screen.getByTestId('control-no-verbs')).toBeInTheDocument();
    });
  });

  it.each(['terminal', 'unavailable'] as const)(
    'offers no controls once the control phase is %s',
    async (phase) => {
      mockGetState.mockResolvedValue(controllableState({ state: phase }));
      renderPanel();
      await waitFor(() => expect(screen.getByTestId('control-phase')).toBeInTheDocument());
      expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
    },
  );

  it('shows no controls while the first state read is still in flight', async () => {
    // A loading skeleton that rendered buttons would be the fail-open case.
    let resolve: (value: ControlStateResponse) => void = () => {};
    mockGetState.mockReturnValue(
      new Promise<ControlStateResponse>((r) => {
        resolve = r;
      }),
    );
    renderPanel();
    expect(screen.getByText(/checking whether this run can be controlled/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
    await act(async () => {
      resolve(controllableState());
    });
  });
});

// ---------------------------------------------------------------------------
// Run identity — no cross-run leakage
// ---------------------------------------------------------------------------

describe('ControlPanel — run identity', () => {
  it('ignores a state response that belongs to a different run', async () => {
    // The specific defect: a poll for the previously selected run resolving
    // after the operator opened a different one, and its capabilities being
    // rendered as though they described the new run.
    mockGetState.mockResolvedValue(controllableState({ run_id: 'some-other-run' }));
    renderPanel({ invocationId: 'run-1' });
    await waitFor(() => {
      expect(screen.getByText(/checking whether this run can be controlled/i)).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
  });

  it('does not reuse one run’s state after the selection switches', async () => {
    mockGetState.mockImplementation((id: string) =>
      Promise.resolve(
        id === 'run-1'
          ? controllableState({ run_id: 'run-1' })
          : controllableState({
              run_id: 'run-2',
              available: false,
              state: 'unavailable',
              capabilities: { pause: false, resume: false, steer: false, abort: false },
              reason: 'run-2 has no control channel',
            }),
      ),
    );
    const { rerender, queryClient } = renderPanel({ invocationId: 'run-1' });
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());

    rerender(
      <QueryClientProvider client={queryClient}>
        <ControlPanel invocationId="run-2" isOpen />
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId('control-unavailable')).toHaveTextContent(/run-2 has no control/i);
    });
    expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
  });

  it('marks locally tracked commands unknown when the worker generation changes', async () => {
    // A generation change means the process that accepted the command is gone.
    // Leaving the command listed as pending would imply it is still queued
    // somewhere, when in truth nothing can now apply it.
    mockGetState.mockResolvedValue(controllableState({ generation: 1 }));
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(screen.getByTestId('command-journal')).toBeInTheDocument());

    mockGetState.mockResolvedValue(controllableState({ generation: 2 }));
    await act(async () => {
      await screen.findByTestId('command-journal');
    });
    await waitFor(
      () => {
        expect(screen.getByTestId('command-journal')).toHaveTextContent(/unknown/i);
      },
      { timeout: 4000 },
    );
  });
});

// ---------------------------------------------------------------------------
// Honest wording
// ---------------------------------------------------------------------------

describe('ControlPanel — honest state wording', () => {
  it('distinguishes pause requested from paused and warns that spend may continue', async () => {
    mockGetState.mockResolvedValue(controllableState({ state: 'pause_requested' }));
    renderPanel();
    await waitFor(() => {
      expect(screen.getByTestId('control-phase')).toHaveTextContent(/pause requested/i);
    });
    expect(screen.getByTestId('control-phase')).not.toHaveTextContent(/^Paused$/);
    expect(screen.getByText(/spend may continue/i)).toBeInTheDocument();
    expect(screen.getByText(/tool call already in progress continues/i)).toBeInTheDocument();
  });

  it('reports a confirmed pause as paused', async () => {
    mockGetState.mockResolvedValue(controllableState({ state: 'paused' }));
    renderPanel();
    await waitFor(() => {
      expect(screen.getByTestId('control-phase')).toHaveTextContent(/^Paused$/);
    });
  });

  it('says an abort is requested but not finished until the run is terminal', async () => {
    mockGetState.mockResolvedValue(controllableState({ state: 'abort_requested' }));
    renderPanel();
    await waitFor(() => {
      expect(screen.getByTestId('control-phase')).toHaveTextContent(/abort requested/i);
    });
    expect(screen.getByText(/not finished until its status shows a terminal outcome/i)).toBeInTheDocument();
  });

  it('reports an unobserved tool count as unknown, never as zero', async () => {
    mockGetState.mockResolvedValue(controllableState({ active_tool_count: null }));
    renderPanel();
    await waitFor(() => {
      expect(screen.getByTestId('active-tools')).toHaveTextContent(/unknown/i);
    });
    expect(screen.getByTestId('active-tools')).not.toHaveTextContent(/: 0/);
  });

  it('distinguishes a reported zero from an unknown count', async () => {
    mockGetState.mockResolvedValue(controllableState({ active_tool_count: 0 }));
    renderPanel();
    await waitFor(() => {
      expect(screen.getByTestId('active-tools')).toHaveTextContent(/none reported/i);
    });
  });

  it('shows a live tool count when the worker reports one', async () => {
    mockGetState.mockResolvedValue(controllableState({ active_tool_count: 3 }));
    renderPanel();
    await waitFor(() => {
      expect(screen.getByTestId('active-tools')).toHaveTextContent(/3/);
    });
  });
});

// ---------------------------------------------------------------------------
// Command status, keyed by command id
// ---------------------------------------------------------------------------

describe('ControlPanel — command acknowledgements', () => {
  it('keys each command status separately so two commands never share a line', async () => {
    mockGetState.mockResolvedValue(
      controllableState({
        commands: [
          { command_id: 'c-a', action: 'pause', status: 'delivered' },
          { command_id: 'c-b', action: 'steer', status: 'pending' },
        ],
      }),
    );
    renderPanel();
    await waitFor(() => expect(screen.getByTestId('command-journal')).toBeInTheDocument());
    expect(screen.getByTestId('command-status-c-a')).toHaveTextContent(/handed to the agent/i);
    expect(screen.getByTestId('command-status-c-b')).toHaveTextContent(/queued/i);
  });

  it('never describes a delivered command as understood, acted on, or applied', async () => {
    mockGetState.mockResolvedValue(
      controllableState({
        commands: [{ command_id: 'c-a', action: 'steer', status: 'delivered' }],
      }),
    );
    renderPanel();
    await waitFor(() => expect(screen.getByTestId('command-status-c-a')).toBeInTheDocument());
    const text = screen.getByTestId('command-status-c-a').textContent ?? '';
    expect(text).toMatch(/not a confirmation/i);
    expect(text).not.toMatch(/\bapplied\b/i);
    expect(text).not.toMatch(/understood|comprehend|accepted by the agent/i);
  });

  it.each([
    ['pending', /queued/i],
    ['delivered', /handed to the agent/i],
    ['applied', /^Applied$/],
    ['cancelled', /cancelled.*not applied/i],
    ['rejected', /rejected.*not applied/i],
    ['unknown', /unknown.*no longer account/i],
  ] as const)('gives command status %s its own honest wording', async (status, pattern) => {
    mockGetState.mockResolvedValue(
      controllableState({ commands: [{ command_id: 'c-x', action: 'pause', status }] }),
    );
    renderPanel();
    await waitFor(() => {
      expect(screen.getByTestId('command-status-c-x')).toHaveTextContent(pattern as RegExp);
    });
  });

  it('shows a locally submitted command before the journal has caught up', async () => {
    // Otherwise a button press produces no visible trace until the next poll,
    // and the operator cannot tell whether their click registered.
    mockGetState.mockResolvedValue(controllableState({ commands: [] }));
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => {
      expect(screen.getByTestId('command-journal')).toHaveTextContent(/queued/i);
    });
  });

  it('lets the server journal supersede the local record for the same command id', async () => {
    mockSendCommand.mockResolvedValue({
      run_id: 'run-1',
      action: 'pause',
      state: 'pause_requested',
      command_id: 'cmd-1',
      command_status: 'pending',
    });
    mockGetState.mockResolvedValue(controllableState({ commands: [] }));
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(screen.getByTestId('command-status-cmd-1')).toBeInTheDocument());

    // Next poll includes the same command, now delivered — one row, not two.
    mockGetState.mockResolvedValue(
      controllableState({
        commands: [{ command_id: 'cmd-1', action: 'pause', status: 'delivered' }],
      }),
    );
    await waitFor(
      () => {
        expect(screen.getByTestId('command-status-cmd-1')).toHaveTextContent(/handed to the agent/i);
      },
      { timeout: 4000 },
    );
    expect(screen.getAllByTestId('command-status-cmd-1')).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

describe('ControlPanel — actions', () => {
  it('sends pause with a freshly minted UUID command id', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(mockSendCommand).toHaveBeenCalled());
    const [runId, action, commandId] = mockSendCommand.mock.calls[0];
    expect(runId).toBe('run-1');
    expect(action).toBe('pause');
    expect(commandId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
  });

  it('sends resume when resume is available', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^resume$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^resume$/i }));
    await waitFor(() => {
      expect(mockSendCommand).toHaveBeenCalledWith('run-1', 'resume', expect.any(String));
    });
  });

  it('requires explicit confirmation before any abort is sent', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^abort$/i })).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: /^abort$/i }));

    // Pressing Abort opens the confirmation and sends nothing.
    expect(screen.getByTestId('abort-confirm')).toBeInTheDocument();
    expect(mockSendCommand).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: /confirm abort/i }));
    await waitFor(() => {
      expect(mockSendCommand).toHaveBeenCalledWith('run-1', 'abort', expect.any(String));
    });
  });

  it('sends nothing when an abort confirmation is cancelled', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^abort$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^abort$/i }));
    await userEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(screen.queryByTestId('abort-confirm')).not.toBeInTheDocument();
    expect(mockSendCommand).not.toHaveBeenCalled();
  });

  it('warns that an abort cannot be undone before confirming', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^abort$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^abort$/i }));
    expect(screen.getByTestId('abort-confirm')).toHaveTextContent(/cannot be resumed/i);
  });

  it('sends a steer instruction and clears the box afterwards', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByLabelText(/send an instruction/i)).toBeInTheDocument());
    const box = screen.getByLabelText(/send an instruction/i);
    await userEvent.type(box, 'use the other branch');
    await userEvent.click(screen.getByRole('button', { name: /send instruction/i }));
    await waitFor(() => {
      expect(mockSendSteer).toHaveBeenCalledWith('run-1', expect.any(String), 'use the other branch');
    });
    await waitFor(() => expect(box).toHaveValue(''));
  });

  it('will not send an empty or whitespace-only instruction', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByLabelText(/send an instruction/i)).toBeInTheDocument());
    const send = screen.getByRole('button', { name: /send instruction/i });
    expect(send).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/send an instruction/i), '   ');
    expect(send).toBeDisabled();
    expect(mockSendSteer).not.toHaveBeenCalled();
  });

  it('makes no comprehension or timing promise about a steer instruction', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByLabelText(/send an instruction/i)).toBeInTheDocument());
    const helper = screen.getByText(/queued for the agent/i);
    expect(helper).toHaveTextContent(/not a guarantee the agent will follow it/i);
    // No ETA may be implied anywhere in the panel.
    expect(screen.queryByText(/will be applied in|within \d+ seconds|estimated/i)).toBeNull();
  });

  it('blocks a duplicate in-flight action so one intent cannot become two', async () => {
    let release: (value: unknown) => void = () => {};
    mockSendCommand.mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());

    const pause = screen.getByRole('button', { name: /^pause$/i });
    await userEvent.click(pause);
    expect(pause).toBeDisabled();
    // Other actions are locked too: a pause and an abort racing each other on
    // the same run is not a state the operator can reason about.
    expect(screen.getByRole('button', { name: /^resume$/i })).toBeDisabled();

    await act(async () => {
      release({
        run_id: 'run-1',
        action: 'pause',
        state: 'pause_requested',
        command_id: 'cmd-1',
        command_status: 'pending',
      });
    });
    expect(mockSendCommand).toHaveBeenCalledTimes(1);
  });

  it('refreshes the run detail after a command so the modal cannot contradict itself', async () => {
    const onCommandApplied = vi.fn();
    renderPanel({ onCommandApplied });
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(onCommandApplied).toHaveBeenCalled());
  });

  it('refreshes the detail again when a later poll shows the run reached a terminal state', async () => {
    // The consequence of an abort does not arrive in the command response: the
    // POST acknowledges `abort_requested` and the run becomes terminal on a
    // later poll. If only the POST triggered a refresh, this panel would read
    // "Finished" beside a detail row still reading in progress.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onCommandApplied = vi.fn();
    mockGetState.mockResolvedValue(controllableState());
    renderPanel({ onCommandApplied });

    await waitFor(() => expect(screen.getByTestId('control-phase')).toHaveTextContent(/running/i));
    const afterInitialRead = onCommandApplied.mock.calls.length;

    // No command sent — purely an observed transition.
    mockGetState.mockResolvedValue(
      controllableState({ state: 'terminal', available: false, capabilities: { pause: false, resume: false, steer: false, abort: false } }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS + 100);
    });

    await waitFor(() => expect(screen.getByTestId('control-phase')).toHaveTextContent(/finished/i));
    expect(onCommandApplied.mock.calls.length).toBeGreaterThan(afterInitialRead);
  });

  it('does not refresh the detail on the first read or on unchanged polls', async () => {
    // Refreshing on every poll would re-fetch the invocation twice a second for
    // as long as the modal stays open.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onCommandApplied = vi.fn();
    renderPanel({ onCommandApplied });

    await waitFor(() => expect(screen.getByTestId('control-phase')).toHaveTextContent(/running/i));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS * 3 + 100);
    });
    expect(onCommandApplied).not.toHaveBeenCalled();
  });

  it('refreshes when a command status advances, so a delivered steer surfaces its effect', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onCommandApplied = vi.fn();
    const journalEntry = (status: string) => ({
      command_id: 'cmd-steer',
      action: 'steer' as const,
      status: status as never,
      accepted_at: null,
      delivered_at: null,
      reason: null,
    });
    mockGetState.mockResolvedValue(controllableState({ commands: [journalEntry('pending')] }));
    renderPanel({ onCommandApplied });

    await waitFor(() => expect(screen.getByTestId('command-journal')).toBeInTheDocument());
    onCommandApplied.mockClear();

    mockGetState.mockResolvedValue(controllableState({ commands: [journalEntry('delivered')] }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS + 100);
    });

    await waitFor(() =>
      expect(screen.getByTestId('command-status-cmd-steer')).toHaveTextContent(
        /handed to the agent/i,
      ),
    );
    expect(onCommandApplied).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

describe('ControlPanel — command errors', () => {
  it.each([
    [409, /cannot accept that command right now/i],
    [410, /already finished.*not applied/i],
    [413, /too long/i],
    [429, /too many commands/i],
    [501, /cannot perform that action yet/i],
    [503, /switched off/i],
    [400, /rejected as invalid/i],
    [404, /not found, or it is not yours/i],
  ])('renders the distinct message for a %i response', async (status, pattern) => {
    mockSendCommand.mockRejectedValue({
      status,
      message: (
        await import('@/services/agentControl')
      ).describeControlError(status as number),
    });
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(pattern as RegExp);
    });
  });

  it('never implies a failed command was applied', async () => {
    mockSendCommand.mockRejectedValue({ status: 410, message: 'This run has already finished. The command was not applied.' });
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByRole('alert')).toHaveTextContent(/not applied/i);
    // No local command row may claim success for a command that was refused.
    expect(screen.queryByTestId('command-journal')).not.toBeInTheDocument();
  });

  it('recovers the controls after a failed command so a retry is possible', async () => {
    mockSendCommand.mockRejectedValue({ status: 429, message: 'Too many commands are already queued for this run. Wait for them to be handled, then retry.' });
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /^pause$/i })).not.toBeDisabled();
  });

  it('says the state is unreadable rather than showing stale state as current', async () => {
    mockGetState.mockRejectedValue({ status: 502, message: 'The run could not be reached. It may have stopped reporting.' });
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText(/could not be reached|could not be read/i)).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
  });

  it('lets the operator dismiss a command error', async () => {
    mockSendCommand.mockRejectedValue({ status: 409, message: 'This run cannot accept that command right now. Its live state may have changed.' });
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Polling lifecycle
// ---------------------------------------------------------------------------

describe('ControlPanel — polling lifecycle', () => {
  it('re-reads control state on its interval while open and visible', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderPanel();
    await waitFor(() => expect(mockGetState).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS * 2);
    });
    // Real calls counted, not an options-object shape: an assertion on
    // `refetchInterval` would pass against a poll that never fires.
    expect(mockGetState.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('stops polling once the run reaches a terminal control phase', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockGetState.mockResolvedValue(controllableState({ state: 'terminal', available: false }));
    renderPanel();
    await waitFor(() => expect(mockGetState).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS * 4);
    });
    expect(mockGetState).toHaveBeenCalledTimes(1);
  });

  it('backs off while the listener is unavailable and observes later abort finalization', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onCommandApplied = vi.fn();
    mockGetState.mockResolvedValue(controllableState({
      available: false, state: 'unavailable',
      capabilities: { pause: false, resume: false, steer: false, abort: false },
    }));
    renderPanel({ onCommandApplied });
    await waitFor(() => expect(mockGetState).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId('control-phase')).toHaveTextContent('Controls unavailable');
    await act(async () => { await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS + 100); });
    expect(mockGetState).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS + 100); });
    expect(mockGetState).toHaveBeenCalledTimes(2);
    mockGetState.mockResolvedValue(controllableState({ state: 'terminal', available: false }));
    await act(async () => { await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS * 4 + 100); });
    expect(screen.getByTestId('control-phase')).toHaveTextContent('Finished');
    expect(onCommandApplied).toHaveBeenCalled();
    const calls = mockGetState.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(mockGetState).toHaveBeenCalledTimes(calls);
  });

  it('stops polling when the modal closes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { rerender, queryClient } = renderPanel();
    await waitFor(() => expect(mockGetState).toHaveBeenCalledTimes(1));
    const callsWhileOpen = mockGetState.mock.calls.length;

    rerender(
      <QueryClientProvider client={queryClient}>
        <ControlPanel invocationId="run-1" isOpen={false} />
      </QueryClientProvider>,
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS * 4);
    });
    expect(mockGetState.mock.calls.length).toBe(callsWhileOpen);
  });

  it('does not poll a hidden tab', async () => {
    // Polling a background tab spends the user's quota on a screen nobody is
    // reading.
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'hidden',
    });
    renderPanel();
    await act(async () => {
      await Promise.resolve();
    });
    expect(mockGetState).not.toHaveBeenCalled();
  });

  it('resumes polling when a hidden tab becomes visible again', async () => {
    let hidden = true;
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => (hidden ? 'hidden' : 'visible'),
    });
    renderPanel();
    expect(mockGetState).not.toHaveBeenCalled();

    hidden = false;
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await waitFor(() => expect(mockGetState).toHaveBeenCalled());
  });

  it('backs off instead of hammering a failing backend', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockGetState.mockRejectedValue({ status: 502, message: 'unreachable' });
    renderPanel();
    await waitFor(() => expect(mockGetState).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CONTROL_POLL_MS * 3);
    });
    // With backoff the second attempt lands no earlier than 2x the base
    // interval, so three base intervals cannot produce three attempts.
    expect(mockGetState.mock.calls.length).toBeLessThan(3);
  });
});

// ---------------------------------------------------------------------------
// Accessibility
// ---------------------------------------------------------------------------

describe('ControlPanel — accessibility', () => {
  it('exposes the controls as a labelled region with a grouped action set', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    expect(screen.getByRole('region', { name: /live run controls/i })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: /run control actions/i })).toBeInTheDocument();
  });

  it('announces the control phase politely as it changes underneath the reader', async () => {
    renderPanel();
    await waitFor(() => expect(screen.getByTestId('control-phase')).toBeInTheDocument());
    const phase = screen.getByTestId('control-phase');
    expect(phase).toHaveAttribute('role', 'status');
    expect(phase).toHaveAttribute('aria-live', 'polite');
  });

  it('announces a failed command through an alert', async () => {
    mockSendCommand.mockRejectedValue({ status: 409, message: 'This run cannot accept that command right now.' });
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());
  });
});

// ---------------------------------------------------------------------------
// Transport hygiene
// ---------------------------------------------------------------------------

describe('ControlPanel — transport hygiene', () => {
  it('never renders a pod address, port or token even if the payload carries one', async () => {
    // The gateway's response models have no field for these, but a worker bug or
    // a future refactor could add one; the panel must not surface it.
    mockGetState.mockResolvedValue({
      ...controllableState(),
      // @ts-expect-error — deliberately injecting fields outside the contract
      control_address: '10.0.42.7',
      control_port: 8099,
      control_token: 'super-secret-token',
    });
    const { container } = renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    expect(container.textContent).not.toContain('10.0.42.7');
    expect(container.textContent).not.toContain('8099');
    expect(container.textContent).not.toContain('super-secret-token');
  });

  it('addresses only the gateway, never a pod-supplied destination', async () => {
    mockGetState.mockResolvedValue(controllableState());
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(mockSendCommand).toHaveBeenCalled());
    // The run id is the only routing input the browser supplies.
    expect(mockSendCommand.mock.calls[0][0]).toBe('run-1');
    expect(mockGetState.mock.calls[0][0]).toBe('run-1');
  });
});

describe('commands across asynchronous state changes', () => {
  it('ignores an old command response after switching runs', async () => {
    let release!: (value: unknown) => void;
    mockSendCommand.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    mockGetState.mockImplementation((id: string) => Promise.resolve(controllableState({ run_id: id })));
    const { rerender, queryClient } = renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: /^pause$/i }));
    rerender(<QueryClientProvider client={queryClient}><ControlPanel invocationId="run-2" isOpen /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByRole('button', { name: /^pause$/i })).not.toBeDisabled());
    await act(async () => release({ run_id: 'run-1', action: 'pause', state: 'pause_requested', command_id: 'old-command', command_status: 'pending' }));
    expect(screen.queryByTestId('command-journal')).not.toBeInTheDocument();
  });

  it('retains an unknown command when its response is lost', async () => {
    mockSendCommand.mockRejectedValue({ status: 0, message: 'The command outcome is unknown.' });
    renderPanel();
    await userEvent.click(await screen.findByRole('button', { name: /^pause$/i }));
    await waitFor(() => expect(screen.getByTestId('command-journal')).toHaveTextContent(/unknown/i));
    expect(screen.queryByText(/it was not applied/i)).not.toBeInTheDocument();
  });

  it('withdraws controls when a refresh fails despite cached running state', async () => {
    const { queryClient } = renderPanel();
    await screen.findByRole('button', { name: /^pause$/i });
    mockGetState.mockRejectedValue({ status: 502, message: 'Unavailable' });
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['agentControl'] }); });
    await waitFor(() => expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument());
    expect(screen.getByRole('alert')).toHaveTextContent(/could not be refreshed/i);
  });
});

it('retains an unresolved command as unknown when the generation changes', async () => {
  let release!: (value: unknown) => void;
  mockSendCommand.mockReturnValue(new Promise((resolve) => { release = resolve; }));
  const { queryClient } = renderPanel();
  await userEvent.click(await screen.findByRole('button', { name: /^pause$/i }));
  mockGetState.mockResolvedValue(controllableState({ generation: 2 }));
  await act(async () => { await queryClient.invalidateQueries({ queryKey: ['agentControl'] }); });
  await waitFor(() => expect(screen.getByTestId('command-journal')).toHaveTextContent(/unknown/i));
  await act(async () => release({ run_id: 'run-1', action: 'pause', state: 'paused', command_id: 'old-command', command_status: 'delivered' }));
  expect(screen.getByTestId('command-journal')).toHaveTextContent(/unknown/i);
  expect(screen.getByTestId('command-journal')).not.toHaveTextContent(/handed to the agent/i);
});


it('shows finished without polling an already terminal invocation', async () => {
  renderPanel({ isTerminalRun: true });
  expect(await screen.findByTestId('control-phase')).toHaveTextContent('Finished');
  expect(mockGetState).not.toHaveBeenCalled();
  expect(screen.queryByText(/checking whether/i)).not.toBeInTheDocument();
});
