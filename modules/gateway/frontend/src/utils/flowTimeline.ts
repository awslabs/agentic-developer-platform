import type { StoryTiming } from '@/types/orchestration';

export function timingRange(timing: StoryTiming, serverTime: string) {
  const start = timing.started_at ? Date.parse(timing.started_at) : NaN;
  const stopped = ['passed', 'failed', 'halted', 'waived', 'rejected_at_gate'].includes(timing.state);
  const end = Date.parse(timing.finished_at || (stopped ? '' : serverTime));
  // A last-updated stamp or worker result must never substitute for a boundary.
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return { start, end, duration: end - start, ongoing: !stopped };
}

export function durationLabel(ms: number) {
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return '<1 min';
  if (minutes < 60) return `${minutes} min`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  return `${Math.floor(minutes / 1440)}d ${Math.floor(minutes % 1440 / 60)}h`;
}

export function timelineState(state: string) {
  const labels: Record<string, string> = { passed: 'Accepted', running: 'In progress', awaiting_merge: 'Awaiting delivery',
    awaiting_gate: 'Awaiting review', rejected_at_gate: 'Changes requested', pending: 'Queued', ready: 'Ready',
    failed: 'Failed', halted: 'Halted', waived: 'Waived' };
  return labels[state] ?? state.replace(/_/g, ' ');
}
