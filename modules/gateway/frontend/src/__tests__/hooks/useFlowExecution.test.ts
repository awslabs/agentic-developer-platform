/**
 * Tests for the execution polling hook — issue #5145.
 *
 * Two of this hook's guarantees are invisible until they fail in production, so
 * they are asserted directly rather than through a rendered page:
 *
 *   - **A stale response must not overwrite a newer one.** Poll responses are not
 *     ordered; React Query's last-write-wins is by *arrival*. With the merge
 *     removed everything still renders, just occasionally one poll out of date —
 *     which on this panel means showing a block that was already cleared, or
 *     "runnable" for work that has since stopped.
 *   - **Polling must stop on navigation.** Asserted at the source level for the
 *     same reason `useFlowGraph` asserts its own settings: the failure is a leak
 *     and a refetch cadence, neither of which a render assertion can see.
 */
import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createElement, type ReactNode } from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  mergeByRevision,
  useFlowExecution,
  FLOW_EXECUTION_REFETCH_INTERVAL_MS,
} from '@/hooks/useFlowExecution';
import { getFlowExecution } from '@/services/orchestration';
import type { ExecutionSummary, FlowExecution } from '@/types/orchestration';

vi.mock('@/services/orchestration', () => ({ getFlowExecution: vi.fn() }));

function summary(overrides: Partial<ExecutionSummary> = {}): ExecutionSummary {
  return {
    id: 'exec-1',
    node_id: 'node-1',
    cycle: 1,
    phase: 'delivering',
    status: 'runnable',
    revision: 5,
    attempts: 1,
    next_check_at: '2026-09-18T12:15:00+00:00',
    deadline_at: null,
    progressed_at: '2026-09-18T11:55:00+00:00',
    progress_note: null,
    block: null,
    pending_action_key: null,
    notification_receipt_ref: null,
    handoff_receipt_ref: null,
    created_at: '2026-09-18T11:00:00+00:00',
    updated_at: '2026-09-18T11:55:00+00:00',
    actions: [],
    action_overflow: false,
    ...overrides,
  };
}

function view(executions: ExecutionSummary[], serverTime = '2026-09-18T12:00:00+00:00'): FlowExecution {
  return {
    flow_id: 'flow-1',
    server_time: serverTime,
    executions,
    total: executions.length,
    limit: 200,
    offset: 0,
    legacy: executions.length === 0,
  };
}

describe('mergeByRevision', () => {
  it('keeps the newer revision when a stale response arrives late', () => {
    // A request issued at t=0 landing after one issued at t=30s. Without this the
    // panel goes backwards and shows a block that has already been cleared.
    const current = view([summary({ revision: 9, status: 'concluded' })]);
    const stale = view([summary({ revision: 5, status: 'blocked' })]);

    const merged = mergeByRevision(current, stale);

    expect(merged.executions[0].revision).toBe(9);
    expect(merged.executions[0].status).toBe('concluded');
  });

  it('accepts a genuinely newer revision', () => {
    // The guard must not freeze the panel. Asserted so "keep the previous value"
    // cannot satisfy the test above by never updating at all.
    const current = view([summary({ revision: 5 })]);
    const fresh = view([summary({ revision: 6, status: 'blocked' })]);

    const merged = mergeByRevision(current, fresh);

    expect(merged.executions[0].revision).toBe(6);
    expect(merged.executions[0].status).toBe('blocked');
  });

  it('accepts an equal revision, since equal is not older', () => {
    const current = view([summary({ revision: 7, progress_note: 'first' })]);
    const same = view([summary({ revision: 7, progress_note: 'second' })]);

    expect(mergeByRevision(current, same).executions[0].progress_note).toBe('second');
  });

  it('merges per execution, because one response can be mixed', () => {
    // A response may legitimately be newer for one node and older for another —
    // they are separate rows written by separate workers. A whole-response
    // comparison would discard the fresh half to protect the stale half.
    const current = view([
      summary({ id: 'a', node_id: 'node-a', revision: 9 }),
      summary({ id: 'b', node_id: 'node-b', revision: 2 }),
    ]);
    const mixed = view([
      summary({ id: 'a', node_id: 'node-a', revision: 4 }),
      summary({ id: 'b', node_id: 'node-b', revision: 8 }),
    ]);

    const merged = mergeByRevision(current, mixed);

    expect(merged.executions.find((e) => e.node_id === 'node-a')?.revision).toBe(9);
    expect(merged.executions.find((e) => e.node_id === 'node-b')?.revision).toBe(8);
  });

  it('keys on node and cycle, so a new cycle is never compared against an old one', () => {
    // Revision counters are per row. Cycle 2 starts at revision 1, which is lower
    // than cycle 1's final revision — comparing across cycles would reject every
    // fresh repair cycle as stale, hiding the retry entirely.
    const current = view([summary({ id: 'c1', cycle: 1, revision: 12 })]);
    const nextCycle = view([summary({ id: 'c2', cycle: 2, revision: 1, phase: 'preparing' })]);

    const merged = mergeByRevision(current, nextCycle);

    expect(merged.executions).toHaveLength(1);
    expect(merged.executions[0].cycle).toBe(2);
    expect(merged.executions[0].revision).toBe(1);
  });

  it('does not resurrect a row the server no longer reports', () => {
    // The page may have moved, or the row may have been dropped as unreadable.
    // Carrying it forward would show an execution the server did not return.
    const current = view([
      summary({ id: 'a', node_id: 'node-a', revision: 9 }),
      summary({ id: 'b', node_id: 'node-b', revision: 9 }),
    ]);
    const shorter = view([summary({ id: 'a', node_id: 'node-a', revision: 10 })]);

    const merged = mergeByRevision(current, shorter);

    expect(merged.executions).toHaveLength(1);
    expect(merged.executions[0].node_id).toBe('node-a');
  });

  it('takes server_time from the newer response even when rows are carried forward', () => {
    // `server_time` timestamps the observation, and every age is computed against
    // it. Keeping the older stamp would misdate the rows that did update.
    const current = view([summary({ revision: 9 })], '2026-09-18T12:00:30+00:00');
    const stale = view([summary({ revision: 5 })], '2026-09-18T12:00:00+00:00');

    expect(mergeByRevision(current, stale).server_time).toBe('2026-09-18T12:00:00+00:00');
  });

  it('returns the incoming object identity when nothing was stale', () => {
    // Structural sharing matters on a 30s poll: a fresh object every tick
    // re-renders every chip in the flow even when nothing changed.
    const incoming = view([summary({ revision: 6 })]);

    expect(mergeByRevision(view([summary({ revision: 5 })]), incoming)).toBe(incoming);
  });

  it('passes the first response through when there is nothing to compare', () => {
    const first = view([summary()]);

    expect(mergeByRevision(undefined, first)).toBe(first);
  });

  it('handles an empty legacy response without inventing rows', () => {
    const empty = view([]);

    expect(mergeByRevision(view([summary({ revision: 9 })]), empty).executions).toEqual([]);
    expect(mergeByRevision(undefined, empty).legacy).toBe(true);
  });
});

describe('the revision memory is scoped to one flow', () => {
  /**
   * The cross-flow carryover regression.
   *
   * Rendered through the real hook rather than asserted on `mergeByRevision`, because
   * the bug is not in the merge — the merge is correct in isolation. It is in the ref
   * that FEEDS the merge: a ref deliberately survives a query-key change, so without a
   * reset the revisions remembered from flow A are still there when flow B's first
   * response arrives.
   *
   * **Both flows reuse the same `(node_id, cycle)` key on purpose.** That is the whole
   * test. `node_id` is a per-flow identifier and `cycle` is a small integer starting at
   * 1, so two flows sharing `(node-shared, 1)` is the common case, not a coincidence. A
   * version of this test using distinct keys passes against the broken hook and proves
   * nothing.
   *
   * Flow A deliberately holds the HIGHER revision, because the guard keeps the higher
   * one — so a leak does not merely flicker, it is sticky: A's row wins every later
   * comparison and persists until unmount instead of being corrected by the next poll.
   */
  it('does not carry a higher-revision row from one flow into another', async () => {
    const responses: Record<string, FlowExecution> = {
      'flow-a': {
        ...view([summary({ id: 'a', node_id: 'node-shared', cycle: 1, revision: 50, progress_note: 'flow-a work' })]),
        flow_id: 'flow-a',
      },
      'flow-b': {
        ...view([summary({ id: 'b', node_id: 'node-shared', cycle: 1, revision: 2, progress_note: 'flow-b work' })]),
        flow_id: 'flow-b',
      },
    };
    vi.mocked(getFlowExecution).mockImplementation(async (flowId: string) => responses[flowId]);

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);

    // Flow A first, so its revision 50 is what the ref remembers.
    const { result, rerender } = renderHook(({ flowId }) => useFlowExecution(flowId), {
      wrapper,
      initialProps: { flowId: 'flow-a' },
    });
    await waitFor(() => expect(result.current.data?.flow_id).toBe('flow-a'));
    expect(result.current.data?.executions[0].revision).toBe(50);

    // Navigate to flow B inside the same mounted hook.
    rerender({ flowId: 'flow-b' });
    await waitFor(() => expect(result.current.data?.flow_id).toBe('flow-b'));

    const shown = result.current.data!.executions[0];
    expect(shown.progress_note).toBe('flow-b work');
    expect(shown.revision).toBe(2);
    expect(shown.progress_note).not.toBe('flow-a work');
  });
});

describe('polling settings', () => {
  /**
   * Source with comments stripped — the same helper and the same reason as
   * `GraphView.test.tsx`'s source-level block.
   *
   * These guards must scan **code**, not prose. Scanning raw text makes
   * documenting a rule violate it: the first version of this file failed because
   * `useFlowExecution.ts`'s docstring explains *why* `usePollingStatus` and the v4
   * `keepPreviousData: true` boolean are wrong. A guard whose only escape is
   * deleting the explanation trains exactly the wrong reflex — the repo has
   * already learned this once, on the graph hook.
   */
  const source = readFileSync(resolve(__dirname, '../../hooks/useFlowExecution.ts'), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1');

  it('polls on an explicit interval rather than relying on staleTime', () => {
    // The app's global staleTime is five minutes. A query relying on cache
    // invalidation shows five-minute-old state on a panel whose entire purpose is
    // "where are we right now" — and it looks live, because the data is real.
    expect(FLOW_EXECUTION_REFETCH_INTERVAL_MS).toBe(30_000);
    expect(source).toMatch(/refetchInterval:\s*FLOW_EXECUTION_REFETCH_INTERVAL_MS/);
  });

  it('forwards the query signal so navigating away cancels the request', () => {
    // This is cancel-on-navigation. React Query aborts the signal when the last
    // observer unmounts; the hook only benefits if the signal reaches the fetch.
    expect(source).toMatch(/queryFn:\s*\(\{\s*signal\s*\}\)\s*=>/);
    expect(source).toMatch(/getFlowExecution\([^)]*signal\)/s);
  });

  it('gates the query on having a flow id', () => {
    // Without `enabled`, the hook fires a request for `undefined` on any route that
    // mounts it without a param.
    expect(source).toMatch(/enabled:\s*Boolean\(flowId\)/);
  });

  it('uses the v5 named keepPreviousData, not the v4 boolean', () => {
    // `keepPreviousData: true` is a silent no-op on the pinned 5.62.0: it type-errors
    // nowhere and simply stops working, so every poll would blank the panel.
    expect(source).toMatch(/placeholderData:\s*keepPreviousData/);
    expect(source).not.toMatch(/keepPreviousData:\s*true/);
  });

  it('does not bind to the shared dashboard polling cadence', () => {
    // Binding this view to `usePollingStatus` lets an unrelated tuning change
    // silently alter delivery freshness.
    expect(source).not.toMatch(/usePollingStatus/);
  });
});
