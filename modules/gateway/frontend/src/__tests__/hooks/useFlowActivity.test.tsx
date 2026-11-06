import { createElement, type ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useFlowActivity } from '@/hooks/useFlowActivity';
import { getFlowActivity } from '@/services/orchestration';
import type { FlowActivity, FlowGraph, GraphNode } from '@/types/orchestration';

const auth = vi.hoisted(() => ({ scope: ['user-a', 'org-a'] as string[] | null }));
vi.mock('@/hooks/useFlowGraph', () => ({ useFlowScope: () => auth.scope }));
vi.mock('@/services/orchestration', () => ({ getFlowActivity: vi.fn() }));

const node = (overrides: Partial<GraphNode> = {}) => ({
  id: 'story', state: 'passed', attempts: 1, run_id: 'run-1', updated_at: '2026-10-26T00:00:00Z', ...overrides,
} as GraphNode);
const graph = (nodes: GraphNode[], flowId = 'flow-a') => ({ activity_included: false, flow_id: flowId, nodes } as FlowGraph);
const response = (nodes: GraphNode[], flowId = 'flow-a'): FlowActivity => ({
  flow_id: flowId, observed_at: '2026-10-26T00:00:00Z', items: nodes.map((node) => ({
    node_id: node.id, attempt: node.attempts, run_id: node.run_id!, availability: 'available',
    execution_history: { run_id: node.run_id!, history_complete: true, activity: null,
      runs: [{ invocation_id: node.run_id!, persona: 'developer', status: 'complete', liveness: 'exited', invoked_at: '2026-10-26T00:00:00Z' }] },
  })),
});

function setup(initialNodes = [node()]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
  return renderHook(({ nodes, updatedAt, flowId }) => useFlowActivity(flowId, graph(nodes, flowId), nodes, updatedAt), {
    wrapper, initialProps: { nodes: initialNodes, updatedAt: 1, flowId: 'flow-a' },
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.mocked(getFlowActivity).mockReset();
  auth.scope = ['user-a', 'org-a'];
  vi.mocked(getFlowActivity).mockResolvedValue(response([node()]));
});

describe('useFlowActivity refreshes', () => {
  it('renders unstarted stories without requests and fetches when an attempt starts', async () => {
    const pending = node({ state: 'pending', attempts: 0, run_id: null });
    const ready = node({ id: 'ready', state: 'ready', attempts: 0, run_id: null });
    const hook = setup([pending, ready]);
    expect(hook.result.current.status(pending)).toBe('not_started');
    expect(hook.result.current.item(ready)?.execution_history).toBeNull();
    expect(getFlowActivity).not.toHaveBeenCalled();
    const running = node({ state: 'running' });
    vi.mocked(getFlowActivity).mockResolvedValue(response([running]));
    hook.rerender({ nodes: [running, ready], updatedAt: 2, flowId: 'flow-a' });
    await waitFor(() => expect(hook.result.current.status(running)).toBe('available'));
    expect(vi.mocked(getFlowActivity).mock.calls[0][1]).toEqual([running.id]);
  });

  it('reuses passed, fully exited history until two minutes then refreshes', async () => {
    const now = Date.now();
    vi.spyOn(Date, 'now').mockReturnValue(now);
    const hook = setup();
    await waitFor(() => expect(hook.result.current.status(node())).toBe('available'));
    for (const seconds of [30, 60, 90]) {
      vi.mocked(Date.now).mockReturnValue(now + seconds * 1000);
      hook.rerender({ nodes: [node()], updatedAt: seconds, flowId: 'flow-a' });
    }
    expect(getFlowActivity).toHaveBeenCalledTimes(1);
    vi.mocked(Date.now).mockReturnValue(now + 120_000);
    hook.rerender({ nodes: [node()], updatedAt: 120, flowId: 'flow-a' });
    await waitFor(() => expect(getFlowActivity).toHaveBeenCalledTimes(2));
  });

  it.each(['running', 'awaiting_merge', 'failed'] as const)('refreshes %s stories even when their developer has completed', async (state) => {
    const story = node({ state });
    const hook = setup([story]);
    await waitFor(() => expect(hook.result.current.status(story)).toBe('available'));
    hook.rerender({ nodes: [story], updatedAt: 2, flowId: 'flow-a' });
    await waitFor(() => expect(getFlowActivity).toHaveBeenCalledTimes(2));
  });

  it.each(['incomplete', 'live'] as const)('refreshes passed stories with %s history', async (kind) => {
    const value = response([node()]);
    if (kind === 'incomplete') value.items[0].execution_history!.history_complete = false;
    else value.items[0].execution_history!.runs[0].liveness = 'live';
    vi.mocked(getFlowActivity).mockResolvedValue(value);
    const hook = setup();
    await waitFor(() => expect(hook.result.current.status(node())).toBe('available'));
    hook.rerender({ nodes: [node()], updatedAt: 2, flowId: 'flow-a' });
    await waitFor(() => expect(getFlowActivity).toHaveBeenCalledTimes(2));
  });

  it('invalidates on state, revision, attempt and run changes', async () => {
    const hook = setup();
    await waitFor(() => expect(hook.result.current.status(node())).toBe('available'));
    const changed = [node({ state: 'running' }), node({ updated_at: '2026-10-26T00:01:00Z' }), node({ attempts: 2 }), node({ run_id: 'run-2' })];
    for (const [index, story] of changed.entries()) {
      vi.mocked(getFlowActivity).mockResolvedValue(response([story]));
      hook.rerender({ nodes: [story], updatedAt: index + 2, flowId: 'flow-a' });
      await waitFor(() => expect(hook.result.current.item(story)).toEqual(response([story]).items[0]));
      expect(getFlowActivity).toHaveBeenCalledTimes(index + 2);
    }
  });

  it('retries failures once on the next refresh and supports manual retry', async () => {
    vi.mocked(getFlowActivity).mockRejectedValueOnce(new Error('unavailable'));
    const hook = setup();
    await waitFor(() => expect(hook.result.current.status(node())).toBe('unavailable'));
    expect(getFlowActivity).toHaveBeenCalledTimes(1);
    hook.rerender({ nodes: [node()], updatedAt: 2, flowId: 'flow-a' });
    await waitFor(() => expect(hook.result.current.status(node())).toBe('available'));
    act(() => hook.result.current.retry(node()));
    await waitFor(() => expect(getFlowActivity).toHaveBeenCalledTimes(3));
  });

  it('invalidates across organization and flow changes and cancels on unmount', async () => {
    const hook = setup();
    await waitFor(() => expect(hook.result.current.status(node())).toBe('available'));
    auth.scope = ['user-a', 'org-b'];
    hook.rerender({ nodes: [node()], updatedAt: 1, flowId: 'flow-a' });
    await waitFor(() => expect(getFlowActivity).toHaveBeenCalledTimes(2));
    vi.mocked(getFlowActivity).mockImplementation(() => new Promise(() => {}));
    hook.rerender({ nodes: [node()], updatedAt: 1, flowId: 'flow-b' });
    await waitFor(() => expect(getFlowActivity).toHaveBeenCalledTimes(3));
    expect(hook.result.current.item(node())).toBeUndefined();
    const signal = vi.mocked(getFlowActivity).mock.calls[2][2]!;
    hook.unmount();
    expect(signal.aborted).toBe(true);
  });

  it('does not accept a late response from an old attempt', async () => {
    let resolveOld!: (response: FlowActivity) => void;
    vi.mocked(getFlowActivity).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    const hook = setup();
    await waitFor(() => expect(getFlowActivity).toHaveBeenCalledTimes(1));
    const next = node({ attempts: 2, run_id: 'run-2' });
    vi.mocked(getFlowActivity).mockResolvedValue(response([next]));
    hook.rerender({ nodes: [next], updatedAt: 2, flowId: 'flow-a' });
    await waitFor(() => expect(hook.result.current.status(next)).toBe('available'));
    await act(async () => resolveOld(response([node()])));
    expect(hook.result.current.item(next)?.run_id).toBe('run-2');
  });

  it('keeps request batches at twenty nodes', async () => {
    const nodes = Array.from({ length: 45 }, (_, index) => node({ id: `story-${index.toString().padStart(2, '0')}` }));
    vi.mocked(getFlowActivity).mockImplementation(async (_flow, ids) => response(nodes.filter((node) => ids.includes(node.id))));
    const hook = setup(nodes);
    await waitFor(() => expect(hook.result.current.status(nodes[44])).toBe('available'));
    expect(vi.mocked(getFlowActivity).mock.calls.map((call) => call[1].length)).toEqual([20, 20, 5]);
  });
});
