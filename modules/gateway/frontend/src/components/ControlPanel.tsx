/**
 * ControlPanel — live run controls (pause / resume / steer / abort) — Issue #3966.
 *
 * Rendered inside the invocation detail modal. Three properties define this
 * component, and each exists because its absence would mislead an operator:
 *
 * 1. **It renders live state, never the snapshot it was opened with.** The
 *    detail modal's `item` is whatever the list held when the row was clicked.
 *    Deriving control availability from it would offer "Pause" on a run that
 *    finished minutes ago. So state is polled per run while the modal is open
 *    and the tab visible, and every control is gated on that polled answer.
 *
 * 2. **It distinguishes requested from applied.** `pause_requested` is not
 *    `paused`; `delivered` is not "the agent acted on it". The copy here never
 *    upgrades one to the other, and says explicitly that a pausing run may still
 *    be running tools and still spending (FR-7.10, FR-7.12, AC-P4).
 *
 * 3. **It fails closed.** Flag off, flags still loading, flags errored,
 *    capability absent, run unavailable, state mismatched — all resolve to "no
 *    control offered". A control that appears when it cannot work is worse than
 *    no control, because someone presses it and believes it worked (AC-F3).
 *
 * The UI asks the backend *what can this run do* and never *which harness is
 * behind it*: there are no provider-name checks here by construction, so a
 * second adapter needs no change to this file (harness-neutral contract).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Textarea } from '@/components/ui';
import { useFeaturesQuery } from '@/hooks/useFeatures';
import {
  MAX_INSTRUCTION_CHARS,
  UNAVAILABLE_STATE,
  getControlState,
  newCommandId,
  sendControlCommand,
  sendSteerCommand,
} from '@/services/agentControl';
import type {
  CommandAcknowledgement,
  CommandStatus,
  ControlAction,
  ControlStateResponse,
} from '@/services/agentControl';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Base poll interval while the modal is open and the tab visible. */
export const CONTROL_POLL_MS = 2000;

/** Ceiling for the error backoff. Bounded so a recovered backend is picked up. */
export const CONTROL_POLL_MAX_MS = 30000;

/** Neither phase allows commands; only terminal ends observation. */
const INERT_STATES: ReadonlySet<string> = new Set(['terminal', 'unavailable']);

// ---------------------------------------------------------------------------
// Presentation helpers — exported for direct unit test
// ---------------------------------------------------------------------------

/**
 * The honest sentence for each control phase.
 *
 * `pause_requested` is the one that matters most: it must not read as "paused".
 * An operator who believes a run is stopped stops watching it, and this run is
 * still executing and still spending.
 */
export function describeControlPhase(state: ControlStateResponse['state']): {
  label: string;
  detail: string | null;
} {
  switch (state) {
    case 'running':
      return { label: 'Running', detail: null };
    case 'pause_requested':
      return {
        label: 'Pause requested — not yet paused',
        detail:
          'The run is still working. A tool call already in progress continues until it finishes, and spend may continue with it.',
      };
    case 'paused':
      return {
        label: 'Paused',
        detail: 'The run is holding at a safe boundary and will not start new work until resumed.',
      };
    case 'abort_requested':
      return {
        label: 'Abort requested — not yet finished',
        detail:
          'The run is shutting down. It is not finished until its status shows a terminal outcome.',
      };
    case 'terminal':
      return { label: 'Finished', detail: 'This run has ended. Controls no longer apply.' };
    case 'unavailable':
    default:
      return { label: 'Controls unavailable', detail: null };
  }
}

/**
 * The honest sentence for one command's status.
 *
 * `delivered` is worded to separate handing over from acting on, and carries no
 * estimate of when anything will happen — the story forbids an ETA or any
 * comprehension claim, because the gateway genuinely cannot know either.
 */
export function describeCommandStatus(status: CommandStatus): string {
  switch (status) {
    case 'pending':
      return 'Queued — not yet handed to the agent';
    case 'delivered':
      return 'Handed to the agent — not a confirmation the agent has acted on it';
    case 'applied':
      return 'Applied';
    case 'cancelled':
      return 'Cancelled — it was not applied';
    case 'rejected':
      return 'Rejected — it was not applied';
    case 'unknown':
    default:
      return 'Unknown — the run can no longer account for this command';
  }
}

/** Human label for a verb. */
const ACTION_LABELS: Record<ControlAction, string> = {
  pause: 'Pause',
  resume: 'Resume',
  steer: 'Send instruction',
  abort: 'Abort',
};

/**
 * Describe how many tools are running, keeping "none" and "unknown" apart.
 *
 * A `null` count rendered as 0 would assert quiescence the worker never
 * observed — the false claim revival-design §4 forbids.
 */
export function describeActiveTools(count: number | null | undefined): string {
  if (typeof count !== 'number') return 'Tools in progress: unknown';
  if (count === 0) return 'Tools in progress: none reported';
  return `Tools in progress: ${count}`;
}

// ---------------------------------------------------------------------------
// Visibility
// ---------------------------------------------------------------------------

/**
 * Track whether the document is visible.
 *
 * Polling a background tab spends the user's quota on a screen nobody is
 * reading, so visibility is an input to `enabled` rather than a nicety.
 */
export function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(
    () => typeof document === 'undefined' || document.visibilityState !== 'hidden',
  );
  useEffect(() => {
    if (typeof document === 'undefined') return;
    const onChange = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', onChange);
    // Re-read on mount: visibility may have changed before the listener attached.
    onChange();
    return () => document.removeEventListener('visibilitychange', onChange);
  }, []);
  return visible;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export interface ControlPanelProps {
  /** The run to control. */
  invocationId: string;
  /**
   * Whether the containing modal is open.
   *
   * Polling is scoped to this rather than to mount so a modal kept mounted while
   * closed does not keep issuing requests.
   */
  isOpen: boolean;
  /**
   * Whether the invocation row is already in a terminal status.
   *
   * A hint used only to avoid pointless polling. It is NOT the gate: a
   * non-terminal status does not imply controllability, so the polled
   * `available` and `capabilities` still decide (revival-design §6).
   */
  isTerminalRun?: boolean;
  /**
   * Called after a command completes, so the containing modal can re-fetch the
   * invocation detail it owns.
   *
   * The panel invalidates the shared activity queries itself, but the detail
   * modal's `item` is passed in as a prop from page state rather than held in a
   * query cache, so only the page can refresh it. Without this, the modal's
   * status row would keep showing the pre-command snapshot.
   */
  onCommandApplied?: () => void;
}

/** One locally submitted command, tracked until the journal reports it. */
interface SubmittedCommand {
  command_id: string;
  action: ControlAction;
  /** Status from the POST response — superseded by the journal once it appears. */
  status: CommandStatus;
  /** Worker generation the command was submitted against. */
  generation: number | null;
}

export function ControlPanel({
  invocationId,
  isOpen,
  isTerminalRun = false,
  onCommandApplied,
}: ControlPanelProps) {
  const queryClient = useQueryClient();
  const visible = useDocumentVisible();

  // The flag is read through the query, not the convenience hook, so that
  // "still loading" and "errored" are distinguishable from "off". All three
  // resolve to hidden, but only an explicit `true` may show controls.
  const { data: features, isPending: featuresPending, isError: featuresError } = useFeaturesQuery();
  const flagEnabled = !featuresPending && !featuresError && features?.agent_control === true;

  const [submitted, setSubmitted] = useState<SubmittedCommand[]>([]);
  const [inFlight, setInFlight] = useState<ControlAction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [instruction, setInstruction] = useState('');
  const [confirmingAbort, setConfirmingAbort] = useState(false);
  const inFlightRef = useRef<string | null>(null);
  const pendingCommandRef = useRef<SubmittedCommand | null>(null);
  const requestScope = useRef({ invocationId, isOpen, generation: null as number | null });
  if (requestScope.current.invocationId !== invocationId || requestScope.current.isOpen !== isOpen) {
    requestScope.current = { invocationId, isOpen, generation: null };
    inFlightRef.current = null;
  }
  const consecutiveFailures = useRef(0);
  const consecutiveUnavailable = useRef(0);

  // Reset every piece of per-run state when the run changes or the modal closes.
  // Without this, commands submitted against one run would be listed under the
  // next run opened — attributing an operator's abort to the wrong run.
  useEffect(() => {
    setSubmitted([]);
    setInFlight(null);
    setActionError(null);
    setInstruction('');
    setConfirmingAbort(false);
    pendingCommandRef.current = null;
    lastGenerationRef.current = null;
    consecutiveFailures.current = 0;
    consecutiveUnavailable.current = 0;
  }, [invocationId, isOpen]);

  const pollEnabled = Boolean(invocationId) && isOpen && visible && flagEnabled && !isTerminalRun;

  const {
    data: rawState,
    isError: stateError,
    error: stateErrorValue,
  } = useQuery({
    // Keyed by run: two runs never share a cache entry, so switching selection
    // cannot show one run's state under another's ID.
    queryKey: ['agentControl', 'state', invocationId],
    queryFn: async ({ signal }) => {
      try {
        const result = await getControlState(invocationId, signal);
        consecutiveFailures.current = 0;
        consecutiveUnavailable.current = result.state === 'unavailable' || !result.available
          ? consecutiveUnavailable.current + 1 : 0;
        return result;
      } catch (error) {
        consecutiveFailures.current += 1;
        throw error;
      }
    },
    enabled: pollEnabled,
    retry: false,
    gcTime: 0,
    refetchInterval: (query) => {
      // Back off on consecutive failures rather than hammering a backend that is
      // already struggling; bounded so recovery is still noticed.
      const current = query.state.data;
      if (current?.state === 'terminal') return false;
      // Listener teardown precedes terminal persistence, and registrations can
      // recover. Unavailable is transient: retry with bounded backoff while visible.
      const failures = Math.max(consecutiveFailures.current, consecutiveUnavailable.current);
      if (failures > 0) {
        return Math.min(CONTROL_POLL_MS * 2 ** failures, CONTROL_POLL_MAX_MS);
      }
      return CONTROL_POLL_MS;
    },
    refetchIntervalInBackground: false,
  });

  /**
   * Discard a response that does not describe the run on screen.
   *
   * The query key already separates runs; this is the second check, against the
   * body itself, so a response that arrived for a previous selection cannot be
   * attributed to the current one.
   */
  const state = useMemo<ControlStateResponse | null>(() => {
    if (!rawState) return isTerminalRun ? { ...UNAVAILABLE_STATE, run_id: invocationId, state: 'terminal' } : null;
    if (rawState.run_id && rawState.run_id !== invocationId) return null;
    return rawState;
  }, [rawState, invocationId, isTerminalRun]);

  const generation = state?.generation ?? null;
  if (requestScope.current.generation !== generation) {
    requestScope.current = { invocationId, isOpen, generation };
    inFlightRef.current = null;
  }

  // A worker generation change means the previous process is gone. Commands
  // submitted against it cannot be reported as delivered — the backend answers
  // `unknown`, and local records from the old generation become unknown so they
  // cannot linger as "pending" forever against a process that no longer exists.
  const lastGenerationRef = useRef<number | null>(null);
  useEffect(() => {
    if (generation === null) return;
    if (lastGenerationRef.current !== null && lastGenerationRef.current !== generation) {
      const unresolved = pendingCommandRef.current;
      pendingCommandRef.current = null;
      setSubmitted((entries) => [...entries, ...(unresolved ? [unresolved] : [])].map((entry) => ({ ...entry, status: 'unknown' })));
      setInFlight(null);
    }
    lastGenerationRef.current = generation;
  }, [generation]);

  /**
   * Refresh the containing modal when the polled state materially changes.
   *
   * `onCommandApplied` at POST time is not enough. The consequences an operator
   * needs to see arrive on a later poll, not in the command response: an abort
   * acknowledges as `abort_requested` and only reaches a terminal status later,
   * and a steer acknowledges as `pending` before the run's own summary reflects
   * it. Without this the panel would say "Finished" next to a detail row still
   * reading in progress — the panel contradicting the record beside it.
   *
   * Keyed on a signature of the phase plus each command's status, so it fires on
   * real transitions and not on every poll that returns the same thing.
   */
  const transitionSignature = state
    ? `${state.state}|${state.available}|${(state.commands ?? [])
        .map((entry) => `${entry.command_id}:${entry.status}`)
        .join(',')}`
    : null;
  const lastSignatureRef = useRef<string | null>(null);
  useEffect(() => {
    if (transitionSignature === null) return;
    const previous = lastSignatureRef.current;
    lastSignatureRef.current = transitionSignature;
    // Skip the first observation: that is the initial read, not a transition,
    // and refreshing on it would re-fetch the detail the modal just opened with.
    if (previous === null || previous === transitionSignature) return;
    if (onCommandApplied) onCommandApplied();
  }, [transitionSignature, onCommandApplied]);

  // Reset the transition baseline when the run changes, so the first poll of a
  // newly opened run is not read as a transition from the previous run's state.
  useEffect(() => {
    lastSignatureRef.current = null;
  }, [invocationId, isOpen]);

  /**
   * Merge the server journal with locally submitted commands.
   *
   * The journal is the truth. The local record exists only to cover the gap
   * between a POST returning and the next poll including it, so the operator is
   * never left with a button press that produced no visible trace.
   */
  const commandRows = useMemo<CommandAcknowledgement[]>(() => {
    const journal = state?.commands ?? [];
    const journalIds = new Set(journal.map((entry) => entry.command_id));
    const localOnly: CommandAcknowledgement[] = submitted
      .filter((entry) => !journalIds.has(entry.command_id))
      .map((entry) => ({
        command_id: entry.command_id,
        action: entry.action,
        status: entry.status,
        accepted_at: null,
        delivered_at: null,
        reason: null,
      }));
    return [...journal, ...localOnly];
  }, [state?.commands, submitted]);

  const capabilities = state?.capabilities;
  const available = state?.available === true;
  const phase = isTerminalRun ? 'terminal' : state?.state ?? 'unavailable';
  const controllable = flagEnabled && !stateError && available && !INERT_STATES.has(phase) && !isTerminalRun;

  /**
   * Whether a specific verb may be offered.
   *
   * Every clause is a separate reason the button could not work. The capability
   * flag is the backend's own intersection of "this deployment implements the
   * verb" with "this run's adapter reports it", so the UI needs no verb list of
   * its own and gains a verb the moment the backend does.
   */
  const can = useCallback(
    (action: ControlAction): boolean =>
      Boolean(controllable && capabilities?.[action] === true),
    [controllable, capabilities],
  );

  const runCommand = useCallback(
    async (action: ControlAction) => {
      // One intent must not become two: a second press while the first is in
      // flight is dropped rather than queued.
      if (inFlightRef.current || !can(action)) return;
      const scope = requestScope.current;
      const requestToken = newCommandId();
      inFlightRef.current = requestToken;
      setInFlight(action);
      setActionError(null);
      const commandId = requestToken;
      pendingCommandRef.current = { command_id: commandId, action, status: 'unknown', generation };
      try {
        const response =
          action === 'steer'
            ? await sendSteerCommand(invocationId, commandId, instruction)
            : await sendControlCommand(invocationId, action, commandId);
        if (requestScope.current !== scope) return;
        setSubmitted((prev) => [
          ...prev,
          {
            command_id: response.command_id || commandId,
            action,
            status: response.command_status ?? 'pending',
            generation,
          },
        ]);
        if (action === 'steer') setInstruction('');
        if (action === 'abort') setConfirmingAbort(false);
        // Refresh both the control state and the invocation lists: a command may
        // have changed the run's status, and leaving the rest of the dashboard on
        // its pre-command snapshot would contradict this panel. The
        // `agent-activity` prefix is the key AgentActivity's run and chain
        // queries share, so one invalidation covers both views.
        await queryClient.invalidateQueries({
          queryKey: ['agentControl', 'state', invocationId],
        });
        await queryClient.invalidateQueries({ queryKey: ['agent-activity'] });
        if (requestScope.current === scope && onCommandApplied) onCommandApplied();
      } catch (error) {
        if (requestScope.current !== scope) return;
        const status = error && typeof error === 'object' && 'status' in error ? Number(error.status) : 0;
        if (status === 0 || status >= 500) {
          setSubmitted((prev) => [...prev, { command_id: commandId, action, status: 'unknown', generation }]);
        }
        const message =
          error && typeof error === 'object' && 'message' in error
            ? String((error as { message: unknown }).message)
            : 'The command outcome could not be confirmed. Check its status before sending it again.';
        setActionError(message);
      } finally {
        if (requestScope.current === scope && inFlightRef.current === requestToken) {
          inFlightRef.current = null;
          pendingCommandRef.current = null;
          setInFlight(null);
        }
      }
    },
    [can, invocationId, instruction, generation, queryClient, onCommandApplied],
  );

  // ---- Render gates -------------------------------------------------------

  // Flag off / still loading / errored: render nothing at all. Not a disabled
  // panel — the feature must be invisible when it is not switched on.
  if (!flagEnabled) return null;
  if (!isOpen) return null;

  // No trustworthy state yet. Deliberately silent rather than showing a
  // skeleton with buttons: a loading state that renders controls is the
  // fail-open this story forbids.
  if (!state) {
    return (
      <section
        aria-label="Live run controls"
        className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-4"
      >
        <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Live controls</h3>
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400" role="status">
          {stateError
            ? (stateErrorValue as { message?: string } | null)?.message ||
              'Live control state could not be read. No command has been sent.'
            : 'Checking whether this run can be controlled…'}
        </p>
      </section>
    );
  }

  const phaseCopy = describeControlPhase(phase);
  const anyCapability = can('pause') || can('resume') || can('steer') || can('abort');

  return (
    <section
      aria-label="Live run controls"
      className="mt-4 border-t border-gray-200 dark:border-gray-700 pt-4 space-y-3"
    >
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Live controls</h3>
        {/* aria-live: the phase changes underneath the reader while they watch. */}
        <span
          className="text-xs font-medium text-gray-700 dark:text-gray-300"
          role="status"
          aria-live="polite"
          data-testid="control-phase"
        >
          {phaseCopy.label}
        </span>
      </div>

      {stateError && (
        <p role="alert" className="text-xs text-amber-700 dark:text-amber-400">
          Live state could not be refreshed. Controls are unavailable until a fresh response arrives.
        </p>
      )}
      {phaseCopy.detail && !stateError && (
        <p className="text-xs text-amber-700 dark:text-amber-400">{phaseCopy.detail}</p>
      )}

      {/* Shown whenever the run is controllable: an operator deciding whether to
          pause needs to know tools may still be running, and that an unknown
          count is unknown rather than zero. */}
      {controllable && (
        <p className="text-xs text-gray-500 dark:text-gray-400" data-testid="active-tools">
          {describeActiveTools(state.active_tool_count)}
        </p>
      )}

      {!available && (
        <p className="text-xs text-gray-500 dark:text-gray-400" data-testid="control-unavailable">
          {state.reason || 'This run has no live control channel, so no controls are offered.'}
        </p>
      )}

      {available && !anyCapability && (
        <p className="text-xs text-gray-500 dark:text-gray-400" data-testid="control-no-verbs">
          No live controls are available for this run in this deployment.
        </p>
      )}

      {actionError && (
        <Alert variant="error" onDismiss={() => setActionError(null)}>
          {actionError}
        </Alert>
      )}

      {anyCapability && (
        <div className="flex flex-wrap gap-2" role="group" aria-label="Run control actions">
          {can('pause') && (
            <Button
              size="sm"
              variant="secondary"
              disabled={inFlight !== null}
              isLoading={inFlight === 'pause'}
              onClick={() => runCommand('pause')}
            >
              {ACTION_LABELS.pause}
            </Button>
          )}
          {can('resume') && (
            <Button
              size="sm"
              variant="secondary"
              disabled={inFlight !== null}
              isLoading={inFlight === 'resume'}
              onClick={() => runCommand('resume')}
            >
              {ACTION_LABELS.resume}
            </Button>
          )}
          {can('abort') && !confirmingAbort && (
            <Button
              size="sm"
              variant="danger"
              disabled={inFlight !== null}
              onClick={() => setConfirmingAbort(true)}
            >
              {ACTION_LABELS.abort}
            </Button>
          )}
        </div>
      )}

      {/* Abort is irreversible, so it takes a second deliberate act. Nothing is
          sent until Confirm is pressed. */}
      {can('abort') && confirmingAbort && (
        <div
          className="rounded border border-red-300 dark:border-red-700 bg-red-50 dark:bg-red-900/20 p-3 space-y-2"
          data-testid="abort-confirm"
        >
          <p className="text-xs text-red-800 dark:text-red-300">
            Abort this run? It will be stopped and cannot be resumed. Work already done is not
            undone.
          </p>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="danger"
              disabled={inFlight !== null}
              isLoading={inFlight === 'abort'}
              onClick={() => runCommand('abort')}
            >
              Confirm abort
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={inFlight !== null}
              onClick={() => setConfirmingAbort(false)}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}

      {can('steer') && (
        <div className="space-y-2">
          <Textarea
            // `id` is required, not cosmetic: the shared Textarea derives its
            // label association from `id || name`, so without one the visible
            // label is not programmatically tied to the field and a screen
            // reader announces an unlabelled text box.
            id="control-steer-instruction"
            label="Send an instruction to this run"
            value={instruction}
            rows={2}
            maxLength={MAX_INSTRUCTION_CHARS}
            onChange={(event) => setInstruction(event.target.value)}
            helperText="Queued for the agent at its next safe boundary. Delivery is not a guarantee the agent will follow it."
          />
          <Button
            size="sm"
            variant="primary"
            disabled={inFlight !== null || instruction.trim().length === 0}
            isLoading={inFlight === 'steer'}
            onClick={() => runCommand('steer')}
          >
            {ACTION_LABELS.steer}
          </Button>
        </div>
      )}

      {commandRows.length > 0 && (
        <div className="space-y-1">
          <h4 className="text-xs font-semibold text-gray-700 dark:text-gray-300">
            Commands you sent
          </h4>
          <ul className="space-y-1" data-testid="command-journal">
            {commandRows.map((entry) => (
              <li
                key={entry.command_id}
                className="text-xs text-gray-600 dark:text-gray-400"
                data-testid={`command-${entry.command_id}`}
              >
                <span className="font-medium">{ACTION_LABELS[entry.action]}</span>
                {': '}
                <span data-testid={`command-status-${entry.command_id}`}>
                  {describeCommandStatus(entry.status)}
                </span>
                {entry.reason && <span className="italic"> — {entry.reason}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

export default ControlPanel;
