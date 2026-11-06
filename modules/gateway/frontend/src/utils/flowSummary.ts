import type { FlowGraph, GraphNode } from '@/types/orchestration';
import { toDisplayState } from './nodeState';

export function intentLabel(ref: string): string {
  const issue = /^https?:\/\/[^\s]+\/issues\/(\d+)\/?$/.exec(ref);
  return issue ? `Intent #${issue[1]}` : /^#?\d+$/.test(ref) ? `Intent #${ref.replace(/^#/, '')}` : ref;
}

export function flowSummary(flow: FlowGraph) {
  const nodes = flow.nodes.filter(node => node.state !== 'superseded');
  const attention = nodes.filter(node => !['passed', 'waived'].includes(node.state) && (toDisplayState(node) === 'stalled' || node.configuration_problem || node.readiness_hold || node.delivery_progress?.automation === 'blocked'));
  const reviews = nodes.filter(node => node.state === 'awaiting_gate');
  const complete = nodes.length > 0 && nodes.every(node => node.state === 'passed' || node.state === 'waived');
  const state = flow.archived_at ? 'Archived' : complete ? 'Complete' : flow.execution_paused ? 'Paused'
    : attention.length ? 'Needs attention' : reviews.length ? 'Awaiting review'
      : nodes.some(node => toDisplayState(node) === 'in_progress') ? 'In progress' : nodes.length ? 'Queued' : 'No work planned';
  return { state, attention, reviews, complete };
}

export function nodeNextStep(node: GraphNode): string {
  const progress = node.delivery_progress;
  return progress?.next_action || progress?.required_input || node.configuration_problem || node.binding_hold
    || progress?.detail || (node.state === 'rejected_at_gate' ? 'Review the feedback and update the plan before reopening review.'
      : 'Open this step to inspect its recorded status and available recovery controls.');
}
