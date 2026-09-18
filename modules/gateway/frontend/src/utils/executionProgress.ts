/**
 * Presentation of the delivery execution read model (issue #5145).
 *
 * The **one shared projection** from `ExecutionSummary` to what an operator reads.
 * Pure and exported so each truthfulness rule below is directly testable — every
 * one of them has a plausible optimistic implementation that renders fine and
 * quietly lies:
 *
 * - **No execution record is not success.** A flow that ran before the ledger
 *   existed has no row and always will have none. `legacy` renders as "no execution
 *   record", never as a clean result and never as an error.
 * - **Blocked is not failed.** It means someone must supply something. Rendered as
 *   an error, it sends an operator hunting a crash that never happened.
 * - **A finished worker is not accepted delivery.** `concluded`/`succeeded` on the
 *   worker's own action says nothing about review, merge, deployment or evaluation.
 *   Only `phase === 'concluded'` with `status === 'concluded'` and no live block is
 *   described as complete.
 * - **An unobserved outcome stays unknown.** `resolved` comes from the server
 *   precisely so `unknown` cannot be upgraded here.
 * - **A green worker status cannot hide an open gate.** `attention` is driven by
 *   the block, so an execution with a successful action and an outstanding gate
 *   still reads as needing attention.
 */

import type {
  ExecutionAction,
  ExecutionPhase,
  ExecutionStatus,
  ExecutionSummary,
  FlowExecution,
} from '@/types/orchestration';

/** How the panel should read. Never `error` for a block — see the module note. */
export type ExecutionTone = 'pending' | 'active' | 'attention' | 'complete' | 'unknown';

export interface EvidenceItem {
  /** The receipt kind, generic across review / merge / deployment / evaluation. */
  kind: string;
  label: string;
  /** Null means pending: the reference is not recorded, or failed sanitisation. */
  reference: string | null;
  /** Free-form status words; `unknown` is preserved as unknown. */
  detail: string;
  resolved: boolean;
}

export interface ExecutionPresentation {
  headline: string;
  tone: ExecutionTone;
  /** Absent unless the execution is actually blocked. */
  blockOwner?: string;
  blockRequiredInput?: string;
  remainingGates: string[];
  /** Plain-language age of the last real progress, e.g. "3 hours ago". */
  lastProgress: string | null;
  /** When the engine will next look, e.g. "in 15 minutes" / "now due". */
  nextCheck: string | null;
  evidence: EvidenceItem[];
  /** Set when older actions were omitted, so a capped list is not read as whole. */
  truncationNote?: string;
  /** Set when earlier cycles exist for this node. */
  cycleNote?: string;
}

const PHASE_LABELS: Record<ExecutionPhase, string> = {
  admitted: 'Accepted for delivery',
  preparing: 'Preparing',
  delivering: 'Delivering',
  submitting: 'Submitting',
  awaiting_review: 'Awaiting review',
  repairing: 'Repairing',
  settling: 'Settling',
  concluded: 'Concluded',
};

const STATUS_LABELS: Record<ExecutionStatus, string> = {
  runnable: 'in progress',
  awaiting_external: 'waiting on an external system',
  blocked: 'blocked',
  concluded: 'finished',
  superseded: 'replaced by a later cycle',
};

/** Who resolves a block, in words an operator can act on. */
const OWNER_LABELS: Record<string, string> = {
  'platform-operator': 'A platform operator',
  'requesting-user': 'You',
  'engine': 'The engine (automatically)',
};

export function phaseLabel(phase: ExecutionPhase): string {
  // Unrecognised phases are shown verbatim rather than mapped to a neighbour: a
  // wrong-but-plausible stage name is worse than an unfamiliar one.
  return PHASE_LABELS[phase] ?? String(phase).replace(/_/g, ' ');
}

export function ownerLabel(owner: string): string {
  return OWNER_LABELS[owner] ?? owner;
}

/**
 * Age of `moment` relative to `serverTime`, both from the server.
 *
 * Deliberately not `Date.now()`: the browser clock may be wrong or in another
 * zone, and it would be wrong on exactly the field an operator acts on ("stuck
 * since when?"). A future instant reads as "just now" rather than a negative age,
 * which would look like a bug in the data.
 */
export function relativeAge(moment: string | null, serverTime: string): string | null {
  if (!moment) return null;
  const then = Date.parse(moment);
  const now = Date.parse(serverTime);
  if (Number.isNaN(then) || Number.isNaN(now)) return null;
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 60) return 'just now';
  return `${describeSpan(seconds)} ago`;
}

/** How long until `moment`, or that it is already due. */
export function relativeDue(moment: string | null, serverTime: string): string | null {
  if (!moment) return null;
  const then = Date.parse(moment);
  const now = Date.parse(serverTime);
  if (Number.isNaN(then) || Number.isNaN(now)) return null;
  const seconds = Math.round((then - now) / 1000);
  // Past-due is stated as such. "in -5 minutes" is unreadable, and silently
  // hiding it would conceal an engine that has stopped picking work up.
  if (seconds <= 0) return 'now due';
  return `in ${describeSpan(seconds)}`;
}

function describeSpan(seconds: number): string {
  const absolute = Math.abs(seconds);
  const units: [number, string][] = [
    [86400, 'day'],
    [3600, 'hour'],
    [60, 'minute'],
  ];
  for (const [size, name] of units) {
    if (absolute >= size) {
      const count = Math.floor(absolute / size);
      return `${count} ${name}${count === 1 ? '' : 's'}`;
    }
  }
  return 'under a minute';
}

/** Status words for one action, preserving `unknown` as unknown. */
export function actionDetail(action: ExecutionAction): string {
  switch (action.status) {
    case 'prepared':
      return 'Prepared, not yet dispatched';
    case 'dispatched':
      return 'Dispatched, outcome not yet observed';
    case 'succeeded':
      return 'Succeeded';
    case 'failed':
      return 'Failed';
    case 'unknown':
      // The engine looked and could not tell. Saying "unknown" is the whole point:
      // an operator must know to go and check rather than assume either way.
      return 'Outcome could not be determined — needs checking';
    default:
      return 'Status not recorded';
  }
}

function evidenceFor(actions: ExecutionAction[]): EvidenceItem[] {
  return actions.map((action) => ({
    kind: action.kind,
    // Generic on purpose: the later review/merge/deployment/evaluation handlers
    // populate `kind` and light this up rather than each needing its own display.
    label: action.kind.replace(/_/g, ' '),
    reference: action.receipt_ref,
    detail: action.receipt_ref
      ? actionDetail(action)
      : // A missing receipt is pending, not absent work. Rendering nothing here
        // makes "the receipt never arrived" look like "there was no step".
        `${actionDetail(action)} · receipt pending`,
    resolved: action.resolved,
  }));
}

/**
 * The newest cycle for one node, plus how many earlier cycles exist.
 *
 * Newest by `cycle`, not by array position or timestamp: a repair cycle is
 * separate work, and showing an earlier cycle's block would send an operator to
 * resolve something the current cycle has already moved past. Earlier cycles are
 * counted rather than hidden, because "this is attempt 3" is itself the answer
 * sometimes.
 */
export function executionForNode(
  view: FlowExecution | undefined,
  nodeId: string
): { execution: ExecutionSummary | null; earlierCycles: number } {
  if (!view) return { execution: null, earlierCycles: 0 };
  const mine = view.executions.filter((execution) => execution.node_id === nodeId);
  if (mine.length === 0) return { execution: null, earlierCycles: 0 };
  const newest = mine.reduce((best, candidate) => (candidate.cycle > best.cycle ? candidate : best));
  return { execution: newest, earlierCycles: mine.length - 1 };
}

/**
 * Project one execution onto its presentation.
 *
 * `serverTime` must be the response's own `server_time` — see `relativeAge`.
 */
export function executionPresentation(
  execution: ExecutionSummary,
  serverTime: string,
  earlierCycles = 0
): ExecutionPresentation {
  const { block } = execution;
  const evidence = evidenceFor(execution.actions);
  const base: ExecutionPresentation = {
    headline: `${phaseLabel(execution.phase)} — ${STATUS_LABELS[execution.status] ?? 'status not recorded'}`,
    tone: 'active',
    remainingGates: block?.remaining_gates ?? [],
    // From the block when blocked: the ledger does not reset `progressed_at` on a
    // block, so this is how long it has actually been stuck.
    lastProgress: relativeAge(block?.progressed_at ?? execution.progressed_at, serverTime),
    nextCheck: relativeDue(execution.next_check_at, serverTime),
    evidence,
    truncationNote: execution.action_overflow
      ? 'Older steps are not shown. This list is the most recent activity, not the whole history.'
      : undefined,
    cycleNote:
      earlierCycles > 0
        ? `Cycle ${execution.cycle}. ${earlierCycles} earlier ${earlierCycles === 1 ? 'cycle' : 'cycles'} not shown here.`
        : undefined,
  };

  if (execution.status === 'blocked' && block) {
    return {
      ...base,
      // Blocked, not failed — and the headline names the resolver, because a status
      // without a next step just sends someone to the logs.
      headline: `Blocked — ${ownerLabel(block.owner).toLowerCase()} must act`,
      tone: 'attention',
      blockOwner: ownerLabel(block.owner),
      blockRequiredInput: block.required_input,
    };
  }

  if (execution.status === 'superseded') {
    return { ...base, headline: 'Replaced by a later cycle', tone: 'unknown' };
  }

  if (execution.status === 'concluded') {
    // Concluded is reached by a cycle that gave up as well as one that delivered,
    // so an unresolved or failed action keeps this out of the "complete" tone.
    const unresolved = execution.actions.some((action) => !action.resolved);
    const failed = execution.actions.some((action) => action.status === 'failed');
    if (failed) {
      return { ...base, headline: 'Finished with a failed step — check the evidence', tone: 'attention' };
    }
    if (unresolved) {
      return {
        ...base,
        headline: 'Finished, but some outcomes were never confirmed',
        tone: 'unknown',
      };
    }
    return { ...base, headline: 'Delivery concluded', tone: 'complete' };
  }

  if (execution.status === 'awaiting_external') {
    return {
      ...base,
      headline: `${phaseLabel(execution.phase)} — waiting on an external system`,
      tone: 'pending',
    };
  }

  return base;
}

/**
 * What to say when a node has no execution row.
 *
 * Two different absences, and conflating them is the error the issue names. A
 * `legacy` flow has no ledger at all and never will — permanent, and emphatically
 * not success. A non-legacy flow whose node has no row simply has not started this
 * node yet. Neither may read as delivered.
 */
export function absentExecutionNote(legacy: boolean): { headline: string; tone: ExecutionTone } {
  return legacy
    ? {
        headline: 'No execution record — this flow ran before delivery tracking existed',
        tone: 'unknown',
      }
    : { headline: 'No delivery activity recorded yet', tone: 'pending' };
}
