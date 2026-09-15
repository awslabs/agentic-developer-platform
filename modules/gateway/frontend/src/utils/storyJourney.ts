import type { GraphNode, StoryActivity } from '@/types/orchestration';

type Stage = 'development' | 'review' | 'fixes' | 'merged';
type Progress = 'upcoming' | 'observed' | 'current' | 'unknown' | 'complete';

export interface JourneyStep {
  id: Stage;
  label: string;
  detail: string;
  progress: Progress;
  run?: StoryActivity;
}

function runDetail(run: StoryActivity): string {
  if (run.status === 'complete') return 'Run finished';
  if (run.liveness === 'exited') return `Run ended: ${run.status.replace(/_/g, ' ')}`;
  if (run.liveness !== 'live') return 'Status unconfirmed';
  if (run.status === 'in_progress') return 'In progress';
  if (run.status === 'webhook_received') return 'Queued';
  return 'Status unconfirmed';
}

/** Story lifecycle only: wave/EPIC rollups continue to use the engine state. */
export function storyJourney(node: GraphNode): { headline: string; steps: JourneyStep[]; historyNote?: string } {
  const steps: JourneyStep[] = [
    { id: 'development', label: 'Development', detail: 'Upcoming', progress: 'upcoming' },
    { id: 'review', label: 'Review', detail: 'Upcoming', progress: 'upcoming' },
    { id: 'fixes', label: 'Fixes', detail: 'If requested · then review again', progress: 'upcoming' },
    { id: 'merged', label: 'Merged (Complete)', detail: 'Upcoming', progress: 'upcoming' },
  ];
  const [development, review, fixes, merged] = steps;
  // A retry gets a fresh journey. Do not reuse cached observations from its
  // predecessor, even if the client receives an old/mixed API response.
  if (node.state === 'pending' || node.state === 'ready') {
    development.detail = node.attempts > 0 ? 'Queued for another attempt' : 'Queued';
    development.progress = 'current';
    return { headline: development.detail, steps };
  }
  if (node.state === 'superseded') {
    steps.forEach(step => { step.detail = 'Replaced'; step.progress = 'unknown'; });
    return { headline: 'Superseded', steps };
  }

  const history = node.run_id && node.execution_history?.run_id === node.run_id ? node.execution_history : null;
  const runs = history?.runs ?? [];
  const canExecute = !node.stalled && ['running', 'awaiting_merge'].includes(node.state);
  const byRun = new Map<string, JourneyStep>();
  let sawReview = false;
  for (const run of runs) {
    const step = run.persona === 'reviewer' ? review : sawReview ? fixes : development;
    if (run.persona === 'reviewer') sawReview = true;
    byRun.set(run.invocation_id, step);
    step.run = run;
    // Incomplete chains can prove that a run ended, never that it is current.
    step.detail = run.liveness === 'exited' || run.status === 'complete' ? runDetail(run) : 'Run recorded';
    step.progress = 'observed';
  }

  if (node.state === 'awaiting_merge' || node.state === 'passed' || sawReview) {
    development.detail = 'Finished';
    development.progress = 'complete';
  }
  if (!review.run) {
    review.detail = 'Status not recorded';
    review.progress = 'unknown';
  }
  if (node.state === 'passed') {
    merged.detail = 'Story complete';
    merged.progress = 'complete';
    if (!fixes.run) {
      fixes.detail = 'History not recorded';
      fixes.progress = 'unknown';
    }
    return { headline: 'Merged — story complete', steps };
  }

  let headline = node.state === 'awaiting_merge' ? 'Awaiting merge' : 'Development status unconfirmed';
  // When history is present it is authoritative for this projection, including
  // an empty/capped response. An older activity field cannot override it.
  const latest = history?.history_complete ? runs[runs.length - 1] : undefined;
  const legacy = node.run_id && node.execution_history == null ? node.activity : null;
  const currentRun = latest ?? (history ? null : legacy);
  if (canExecute && currentRun) {
    const step = byRun.get(currentRun.invocation_id) ?? (currentRun.persona === 'reviewer'
      ? review
      : node.state === 'awaiting_merge' && currentRun.invocation_id !== node.run_id ? fixes : development);
    step.run = currentRun;
    step.detail = runDetail(currentRun);
    step.progress = 'current';
    if (step === development && !review.run && node.state === 'running') {
      review.detail = 'Upcoming';
      review.progress = 'upcoming';
    }
    if (step === review || step === fixes) {
      development.detail = 'Finished';
      development.progress = 'complete';
    }
    if (currentRun.status === 'complete') {
      if (step === review) {
        headline = 'Review finished — check feedback';
        step.detail = 'Run finished · check feedback';
      } else if (step === fixes) {
        headline = 'Fixes finished — review next';
        review.detail = 'Review again next';
      } else {
        headline = 'Development finished — review next';
        review.detail = 'Upcoming';
        review.progress = 'upcoming';
      }
    } else if (currentRun.liveness === 'exited') {
      headline = `${step.label} run ended — check run`;
    } else {
      headline = `${step.label} ${step.detail.toLowerCase()}`;
      if (step === fixes) review.detail = review.run ? 'Earlier run recorded · review again after fixes' : 'Review again after fixes';
    }
  }
  if (!canExecute) {
    headline = node.stalled ? 'Stalled — needs help'
      : node.state === 'halted' ? 'Halted'
        : node.state === 'failed' ? 'Failed'
          : node.state === 'rejected_at_gate' ? 'Changes requested at gate'
            : node.state === 'awaiting_gate' ? 'Waiting on a gate' : 'Story status unconfirmed';
  }
  return {
    headline, steps,
    historyNote: history?.history_complete ? undefined : 'Stage history is incomplete; unrecorded work is not marked finished.',
  };
}
