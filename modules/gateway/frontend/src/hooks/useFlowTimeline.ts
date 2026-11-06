import { useQuery } from '@tanstack/react-query';
import { getFlowTimeline } from '@/services/orchestration';
import { useFlowPollInterval } from '@/hooks/useFlowPollInterval';

export function useFlowTimeline(flowId: string, offset: number, enabled: boolean) {
  const interval = useFlowPollInterval(30_000);
  return useQuery({
    queryKey: ['orchestration', 'flow-timeline', flowId, offset],
    queryFn: ({ signal }) => getFlowTimeline(flowId, offset, signal),
    enabled, refetchInterval: enabled ? interval : false, refetchOnWindowFocus: enabled ? 'always' : false,
    refetchIntervalInBackground: false,
  });
}
