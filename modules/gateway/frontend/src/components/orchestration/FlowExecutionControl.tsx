import { useMutation, useQueryClient } from '@tanstack/react-query';
import { usePermissions } from '@/hooks/usePermissions';
import { Permission } from '@/types';
import { apiClient } from '@/services/api';
import { Badge, Button } from '@/components/ui';

export function FlowExecutionControl({ flowId, paused, compact = false }: {
  flowId: string;
  paused: boolean | undefined;
  compact?: boolean;
}) {
  const { hasPermission } = usePermissions();
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: (value: boolean) => apiClient.post(`/orchestration/flows/${encodeURIComponent(flowId)}/execution`, { paused: value }),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['orchestration'] }); },
  });

  return <section aria-label="Flow execution" className="space-y-2">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <span className="text-sm text-gray-700 dark:text-gray-300">
        Execution: {paused === undefined ? 'Control unavailable' : <Badge variant={paused ? 'warning' : 'info'}>{paused ? 'Paused' : 'Enabled'}</Badge>}
      </span>
      {paused !== undefined && hasPermission(Permission.PLAN_APPROVE) && <Button
        variant="secondary" size="sm" disabled={mutation.isPending}
        onClick={() => mutation.mutate(!paused)}
      >{mutation.isPending ? 'Saving…' : paused ? 'Resume flow' : 'Pause flow'}</Button>}
    </div>
    {!compact && <p className="text-sm text-gray-600 dark:text-gray-400">
      Pause prevents new agent runs and retries. Work already queued or starting can finish.
      {' '}Resume preserves progress and attempts; the global engine must also be enabled.
    </p>}
    {mutation.isError && <p role="alert" className="text-sm text-red-600">The change was not saved. Try again.</p>}
  </section>;
}
