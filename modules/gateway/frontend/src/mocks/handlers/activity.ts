/**
 * MSW handlers for Agent Activity endpoints.
 *
 * Issue #1457: Phase 3 — Frontend "Agent Activity" page.
 * Simulates cursor-based pagination with DynamoDB-style last_key.
 */

import { http, HttpResponse } from 'msw';
import { mockInvocations } from '../data/activity';
import type { InvocationItem } from '@/types/activity';

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
];
