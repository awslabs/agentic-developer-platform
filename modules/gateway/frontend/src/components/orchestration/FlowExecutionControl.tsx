import { useMutation, useQueryClient } from '@tanstack/react-query';
import { usePermissions } from '@/hooks/usePermissions';
import { Permission } from '@/types';
import { apiClient } from '@/services/api';
import { Badge, Button } from '@/components/ui';

export function FlowExecutionControl({ flowId, paused, recoveryEnabled, compact = false }: {
  flowId: string;
  paused: boolean | undefined;
  recoveryEnabled?: boolean;
  compact?: boolean;
}) {
  const { hasPermission } = usePermissions();
  const client = useQueryClient();
  const mutation = useMutation({
    mutationFn: (value: boolean) => apiClient.post(`/orchestration/flows/${encodeURIComponent(flowId)}/execution`, { paused: value }),
    onSuccess: async () => { await client.invalidateQueries({ queryKey: ['orchestration'] }); },
  });
  const recovery = useMutation({
    mutationFn: (enabled: boolean) => apiClient.post(`/orchestration/flows/${encodeURIComponent(flowId)}/recovery`, { enabled }),
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
    {recoveryEnabled !== undefined && <div className="space-y-2 border-t border-gray-200 pt-2 dark:border-gray-700">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-sm text-gray-700 dark:text-gray-300">Automatic retry recovery: <Badge variant={recoveryEnabled ? 'info' : 'warning'}>{recoveryEnabled ? 'On' : 'Off'}</Badge></span>
        {hasPermission(Permission.PLAN_APPROVE) && <Button variant="secondary" size="sm" disabled={recovery.isPending}
          onClick={() => recovery.mutate(!recoveryEnabled)}>{recovery.isPending ? 'Saving recovery…' : recoveryEnabled ? 'Turn recovery off' : 'Turn recovery on'}</Button>}
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400">Retries use saved progress within approved attempts, time and budgets. Turning recovery off prevents new automatic retries; observation and work already started continue. Paused flows stay paused.</p>
      {recovery.isError && <p role="alert" className="text-sm text-red-600">The recovery setting was not saved. Try again.</p>}
    </div>}
  </section>;
}
