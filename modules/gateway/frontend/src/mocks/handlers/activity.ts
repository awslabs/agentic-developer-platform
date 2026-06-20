/**
 * MSW handlers for Agent Activity endpoints.
 *
 * Issue #1457: Phase 3 — Frontend "Agent Activity" page.
 * Issue #1461: Phase 6 — Chain view endpoint for lineage.
 * Simulates cursor-based pagination with DynamoDB-style last_key.
 */

import { http, HttpResponse } from 'msw';
import { mockInvocations } from '../data/activity';
import type { InvocationItem, InvocationChainItem, InvocationChainResponse } from '@/types/activity';

function filterAndPaginate(request: Request, items: InvocationItem[]) {
  const url = new URL(request.url);
  const status = url.searchParams.get('status');
  const channel = url.searchParams.get('channel');
  const persona = url.searchParams.get('persona');
  const startDate = url.searchParams.get('start_date');
  const endDate = url.searchParams.get('end_date');
  const limit = parseInt(url.searchParams.get('limit') || '20');
  const lastKey = url.searchParams.get('last_key');

  let filtered = [...items];

  if (status) {
    filtered = filtered.filter((inv) => inv.status === status);
  }
  if (channel) {
    filtered = filtered.filter((inv) => inv.channel === channel);
  }
  if (persona) {
    filtered = filtered.filter((inv) => inv.persona === persona);
  }
  if (startDate) {
    filtered = filtered.filter((inv) => inv.invoked_at >= startDate);
  }
  if (endDate) {
    filtered = filtered.filter((inv) => inv.invoked_at <= endDate);
  }

  // Sort by invoked_at descending (newest first)
  filtered.sort(
    (a, b) => new Date(b.invoked_at).getTime() - new Date(a.invoked_at).getTime(),
  );

  // Cursor pagination: find offset from last_key
  let startIdx = 0;
  if (lastKey) {
    const keyIdx = filtered.findIndex((inv) => inv.invocation_id === lastKey);
    if (keyIdx >= 0) {
      startIdx = keyIdx + 1;
    }
  }

  const pageItems = filtered.slice(startIdx, startIdx + limit);
  const hasMore = startIdx + limit < filtered.length;

  return HttpResponse.json({
    items: pageItems,
    last_key: hasMore ? pageItems[pageItems.length - 1]?.invocation_id ?? null : null,
  });
}

function buildChainResponse(correlationId: string, items: InvocationItem[]): InvocationChainResponse {
  // Find all items with this correlation_id
  const chainItems = items.filter((inv) => inv.correlation_id === correlationId);

  // Build tree structure
  const nodes: Map<string, InvocationChainItem> = new Map();
  for (const item of chainItems) {
    nodes.set(item.invocation_id, {
      invocation_id: item.invocation_id,
      invoked_at: item.invoked_at,
      channel: item.channel,
      status: item.status,
      topic: item.topic,
      persona: item.persona,
      parent_invocation_id: item.triggered_by_invocation_id,
      children: [],
      // Issue #1653: per-node cost carried from the source item
      total_cost_usd: item.total_cost_usd,
      total_tokens: item.total_tokens,
      call_count: item.call_count,
    });
  }

  // Link children to parents
  const roots: InvocationChainItem[] = [];
  for (const node of nodes.values()) {
    if (node.parent_invocation_id && nodes.has(node.parent_invocation_id)) {
      nodes.get(node.parent_invocation_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }

  // Determine root_human_id from first human-rooted item
  const firstHumanRooted = chainItems.find((i) => i.is_human_rooted);

  // Issue #1653: chain-wide cost totals (sum across all nodes in the chain)
  const chainTotalCost = chainItems.reduce((s, i) => s + (i.total_cost_usd ?? 0), 0);
  const chainTotalTokens = chainItems.reduce((s, i) => s + (i.total_tokens ?? 0), 0);
  const chainTotalCalls = chainItems.reduce((s, i) => s + (i.call_count ?? 0), 0);

  return {
    correlation_id: correlationId,
    root_human_id: firstHumanRooted?.root_human_id ?? null,
    is_human_rooted: firstHumanRooted?.is_human_rooted ?? false,
    items: roots,
    total_count: chainItems.length,
    depth_capped: false,
    chain_total_cost_usd: chainTotalCost,
    chain_total_tokens: chainTotalTokens,
    chain_total_call_count: chainTotalCalls,
  };
}

export const activityHandlers = [
  // User's own invocations
  http.get('/api/me/agent-invocations', ({ request }) => {
    // In mock, filter to a single user to simulate "mine"
    const myItems = mockInvocations.filter((inv) => inv.user_id === 'user-001');
    return filterAndPaginate(request, myItems);
  }),

  // Admin: all invocations
  http.get('/api/admin/agent-invocations', ({ request }) => {
    return filterAndPaginate(request, mockInvocations);
  }),

  // User's chain view
  http.get('/api/me/agent-invocations/chain/:correlationId', ({ params }) => {
    const correlationId = params.correlationId as string;
    const myItems = mockInvocations.filter((inv) => inv.user_id === 'user-001');
    return HttpResponse.json(buildChainResponse(correlationId, myItems));
  }),

  // Admin chain view
  http.get('/api/admin/agent-invocations/chain/:correlationId', ({ params }) => {
    const correlationId = params.correlationId as string;
    return HttpResponse.json(buildChainResponse(correlationId, mockInvocations));
  }),
];
