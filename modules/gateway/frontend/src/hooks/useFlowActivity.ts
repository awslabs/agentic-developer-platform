import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getFlowActivity } from '@/services/orchestration';
import { useFlowScope } from '@/hooks/useFlowGraph';
import type { FlowActivityItem, FlowGraph, GraphNode } from '@/types/orchestration';

interface ActivityState {
  identity: string;
  items: Record<string, { item: FlowActivityItem; updatedAt: number; fetchedAt: number }>;
  failed: Record<string, number>;
  retryVersion: number;
}

const COMPLETED_HISTORY_REFRESH_MS = 120_000;
const nodeIdentity = (node: GraphNode) => JSON.stringify([
  node.id, node.attempts, node.run_id ?? null, node.state, node.updated_at ?? null,
]);

export function useFlowActivity(flowId: string | undefined, graph: FlowGraph | undefined, visibleNodes: GraphNode[], graphUpdatedAt: number) {
  const scope = useFlowScope();
  const identity = JSON.stringify([scope, flowId]);
  const [stored, setStored] = useState<ActivityState>({ identity, items: {}, failed: {}, retryVersion: 0 });
  const state = stored.identity === identity ? stored : { identity, items: {}, failed: {}, retryVersion: 0 };
  const nodes = graph?.activity_included === false && graph.flow_id === flowId && scope
    ? [...new Map(visibleNodes.map((node) => [node.id, node])).values()].sort((first, second) => first.id.localeCompare(second.id)) : [];
  const unstarted = new Set(nodes.filter((node) =>
    node.attempts === 0 && !node.run_id && (node.state === 'pending' || node.state === 'ready')
  ).map(nodeIdentity));
  const unstartedItem = (node: GraphNode): FlowActivityItem => ({
    node_id: node.id, attempt: node.attempts, run_id: null, availability: 'not_started', execution_history: null,
  });
  const batch = nodes.filter((node) => {
    const key = nodeIdentity(node);
    if (unstarted.has(key)) return false;
    if (state.failed[key] === graphUpdatedAt) return false;
    if (state.failed[key] !== undefined) return true;
    const previous = state.items[key];
    if (!previous) return true;
    if (previous.updatedAt === graphUpdatedAt) return false;
    const history = previous.item.execution_history;
    // A completed developer can still acquire reviewer/repair children in the
    // same attempt. Only passed stories with fully exited history get a longer
    // refresh interval; active stories continue following every graph refresh.
    const complete = node.state === 'passed' && history?.history_complete &&
      !history.activity && history.runs.length > 0 && history.runs.every((run) => run.liveness === 'exited');
    return !complete || Date.now() - previous.fetchedAt >= COMPLETED_HISTORY_REFRESH_MS;
  }).slice(0, 20);
  const batchKey = JSON.stringify(batch.map(nodeIdentity));
  const query = useQuery({
    queryKey: ['orchestration', 'flow-activity', identity, batchKey, graphUpdatedAt, state.retryVersion],
    queryFn: ({ signal }) => getFlowActivity(flowId as string, batch.map((node) => node.id), signal),
    enabled: batch.length > 0,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    gcTime: 0,
  });

  useEffect(() => {
    if (!batch.length || (!query.isSuccess && !query.isError)) return;
    const response = query.data;
    const valid = response !== null && typeof response === 'object' && response.flow_id === flowId && Array.isArray(response.items) && response.items.length === batch.length &&
      batch.every((node, index) => {
        const item = response.items[index];
        return item?.node_id === node.id && item.attempt === node.attempts &&
          item.run_id === (node.run_id ?? null) &&
          (item.availability === 'not_started' ? item.run_id === null && item.execution_history === null
            : item.availability === 'unavailable' ? !!item.run_id && item.execution_history === null
              : item.availability === 'available' && !!item.run_id && item.execution_history?.run_id === item.run_id);
      });
    setStored((current) => {
      const next = current.identity === identity ? current : { identity, items: {}, failed: {}, retryVersion: 0 };
      if (!valid) {
        return { ...next, failed: { ...next.failed, ...Object.fromEntries(batch.map((node) => [nodeIdentity(node), graphUpdatedAt])) } };
      }
      const failed = { ...next.failed };
      batch.forEach((node, index) => {
        if (response!.items[index].availability === 'unavailable') failed[nodeIdentity(node)] = graphUpdatedAt;
        else delete failed[nodeIdentity(node)];
      });
      return {
        ...next,
        items: { ...next.items, ...Object.fromEntries(batch.map((node, index) => [nodeIdentity(node), {
          item: response!.items[index], updatedAt: graphUpdatedAt, fetchedAt: query.dataUpdatedAt,
        }])) },
        failed,
      };
    });
  }, [batch, batchKey, graphUpdatedAt, identity, flowId, query.data, query.dataUpdatedAt, query.isSuccess, query.isError]);

  return {
    status: (node: GraphNode) => {
      const key = nodeIdentity(node);
      if (unstarted.has(key)) return 'not_started' as const;
      if (state.failed[key] !== undefined) return 'unavailable' as const;
      const item = state.items[key]?.item;
      return item ? item.availability : 'loading' as const;
    },
    item: (node: GraphNode) => unstarted.has(nodeIdentity(node)) ? unstartedItem(node) : state.items[nodeIdentity(node)]?.item,
    retry: (node: GraphNode) => setStored((current) => {
      const next = current.identity === identity ? current : { identity, items: {}, failed: {}, retryVersion: 0 };
      const failed = { ...next.failed };
      const items = { ...next.items };
      delete failed[nodeIdentity(node)];
      delete items[nodeIdentity(node)];
      return { ...next, items, failed, retryVersion: next.retryVersion + 1 };
    }),
  };
}
