/**
 * Mock data for Agent Activity page.
 *
 * Issue #1457: Phase 3 — Frontend "Agent Activity" page.
 * Issue #1461: Phase 6 — lineage fields (trigger_kind, parent, chain).
 */

import type { InvocationItem, InvocationStatus, InvocationChannel, TriggerKind } from '@/types/activity';

const statuses: InvocationStatus[] = [
  'webhook_received',
  'in_progress',
  'complete',
  'complete',
  'complete',
  'failed',
  'rejected',
  'rate_limited',
  'no_op',
];

const channels: InvocationChannel[] = ['github', 'github', 'github', 'slack', 'api'];

const personas = ['developer', 'architect', 'reviewer', 'ops'];

const topics = [
  'Implement Agent Activity page',
  'Fix CORS headers on API Gateway',
  'Add rate limit to /v1/chat endpoint',
  'Refactor auth middleware',
  'Update deployment manifest',
  'Add integration tests for webhook',
  'Fix flaky CI test',
  'Upgrade Node.js to v22',
  null,
];

const repos = ['aws-e/adp', 'aws-e/infra-core', 'aws-e/gateway-plugins', null];

// Chain correlation IDs for lineage demo
const correlationIds = ['chain-001', 'chain-001', 'chain-001', 'chain-002', 'chain-002', null];

export function generateMockInvocations(count: number = 30): InvocationItem[] {
  return Array.from({ length: count }, (_, i) => {
    const status = statuses[Math.floor(Math.random() * statuses.length)];
    const channel = channels[Math.floor(Math.random() * channels.length)];
    const repo = repos[Math.floor(Math.random() * repos.length)];
    const issueNumber = channel === 'github' && repo ? Math.floor(Math.random() * 1500) + 1 : null;
    const invokedAt = new Date(Date.now() - Math.random() * 14 * 24 * 60 * 60 * 1000);
    const isTerminal = ['complete', 'failed', 'rejected', 'rate_limited', 'no_op'].includes(status);
    const correlationId = correlationIds[Math.floor(Math.random() * correlationIds.length)];

    // Derive trigger_kind: first few items in a chain are human, rest are agent-triggered
    let triggerKind: TriggerKind = 'human';
    let parentInvocationId: string | null = null;
    let parentTopic: string | null = null;

    if (i > 0 && i % 4 !== 0 && correlationId) {
      // Agent-triggered (has parent)
      triggerKind = 'agent';
      parentInvocationId = `inv-${String(i - 1).padStart(6, '0')}`;
      parentTopic = topics[Math.floor(Math.random() * (topics.length - 1))];
    } else if (i % 7 === 0) {
      // Bot/cron (not human-rooted)
      triggerKind = 'bot';
    }

    return {
      invocation_id: `inv-${String(i + 1).padStart(6, '0')}`,
      user_id: `user-${String((i % 5) + 1).padStart(3, '0')}`,
      persona: personas[Math.floor(Math.random() * personas.length)],
      channel,
      status,
      topic: topics[Math.floor(Math.random() * topics.length)],
      summary: status === 'complete' ? `Completed work on issue #${issueNumber || i + 1}` : null,
      source_url:
        channel === 'github' && repo && issueNumber
          ? `https://github.com/${repo}/issues/${issueNumber}`
          : null,
      repo,
      issue_number: issueNumber,
      invoked_at: invokedAt.toISOString(),
      completed_at: isTerminal
        ? new Date(invokedAt.getTime() + Math.random() * 30 * 60 * 1000).toISOString()
        : null,
      status_updated_at: new Date(invokedAt.getTime() + Math.random() * 5 * 60 * 1000).toISOString(),
      run_id: `agent-scaledjob-${String(i + 1).padStart(5, '0')}`,
      // Phase 6 lineage fields
      trigger_kind: triggerKind,
      triggered_by_invocation_id: parentInvocationId,
      triggered_by_topic: parentTopic,
      root_human_id: triggerKind === 'bot' ? null : 'user-001',
      is_human_rooted: triggerKind !== 'bot',
      correlation_id: correlationId,
      // Issue #1616: per-run cost (present for runs that made model calls)
      total_cost_usd: isTerminal ? Math.round(Math.random() * 5000) / 1000 : null,
      total_tokens: isTerminal ? Math.floor(Math.random() * 200000) : null,
      call_count: isTerminal ? Math.floor(Math.random() * 40) + 1 : null,
      // Error detail for failed runs (drives the detail view)
      error_message: status === 'failed' ? 'Model access error: throttled by Bedrock' : null,
      // Issue #1653: run log link (Tier 2 — null until worker persists check_run_url)
      run_log_url: null,
      // Issue #3069: S3 transcript key (present for completed runs after #3061)
      transcript_key: isTerminal && status === 'complete'
        ? `developer/aws-e/adp/issue-${issueNumber || i + 1}/20260706T150000Z-${String(i + 1).padStart(8, '0')}.md`
        : null,
    };
  });
}

export const mockInvocations = generateMockInvocations(30);
