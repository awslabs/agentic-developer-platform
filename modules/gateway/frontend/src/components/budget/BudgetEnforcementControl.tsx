import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuthContext } from '@/contexts/AuthContext';
import { usePermissions } from '@/hooks/usePermissions';
import { apiClient } from '@/services/api';

export interface BudgetEnforcementStatus {
  global_enabled: boolean;
  flow_enabled: boolean | null;
  effective_enabled: boolean;
  accounting_incomplete: boolean;
  revision: number;
}

/** The global switch is a master switch; each flow may opt out independently. */
export function BudgetEnforcementControl({ flowId }: { flowId?: string }) {
  const { user } = useAuthContext();
  const { isPlatformAdmin } = usePermissions();
  const client = useQueryClient();
  const endpoint = flowId ? `/budget/enforcement/flows/${encodeURIComponent(flowId)}` : '/budget/enforcement';
  const queryKey = ['budget-enforcement', user?.orgId, flowId ?? 'global'];
  const query = useQuery({
    queryKey,
    queryFn: () => apiClient.get<BudgetEnforcementStatus>(endpoint),
    staleTime: 0,
    refetchInterval: 15_000,
  });
  const mutation = useMutation({
    mutationFn: (enabled: boolean) => apiClient.post<BudgetEnforcementStatus>(endpoint, {
      enabled,
      expected_revision: query.data!.revision,
      reason: `Administrator switched budget enforcement ${enabled ? 'on' : 'off'} ${flowId ? 'for this flow' : 'globally'} from the budget control.`,
    }),
    onSuccess: (value) => {
      client.setQueryData(queryKey, value);
      void client.invalidateQueries({ queryKey: ['budget-enforcement'] });
    },
    onError: () => { void query.refetch(); },
  });
  const selected = flowId ? query.data?.flow_enabled !== false : query.data?.global_enabled;

  return (
    <section aria-label={flowId ? 'Flow budget enforcement' : 'Global budget enforcement'} className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-semibold text-gray-900 dark:text-white">{flowId ? 'Flow budget enforcement' : 'Global budget enforcement'}</h2>
        {query.data && <span className="text-sm">Effective: <strong>{query.data.effective_enabled ? 'On' : 'Off'}</strong></span>}
      </div>
      <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">
        {flowId ? 'Switch off spending limits for this flow.' : 'Switch off spending limits across ADP, including all flows.'}
        {' '}Usage and costs continue to be recorded. Attempt limits, expiry, permissions and review approvals still apply.
      </p>
      {query.isPending && <p className="mt-2 text-sm">Loading budget setting…</p>}
      {query.isError && <p role="alert" className="mt-2 text-sm text-red-600">Budget setting could not be loaded. <button type="button" className="underline" onClick={() => void query.refetch()}>Retry</button></p>}
      {query.data && <>
        {isPlatformAdmin() ? <label className="mt-3 flex items-center gap-2 text-sm">
          <input type="checkbox" role="switch" checked={Boolean(selected)} disabled={mutation.isPending || query.isError}
            onChange={(event) => mutation.mutate(event.target.checked)} />
          {flowId ? 'Enforce budgets for this flow when global enforcement is on' : 'Enforce budgets globally'}
        </label> : <p className="mt-3 text-sm">A platform administrator can change this setting.</p>}
        {flowId && !query.data.global_enabled && <p className="mt-2 text-sm">Global enforcement is off, so this flow's budgets are currently off too.</p>}
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">Changes apply to the next model request or engine admission. Turning enforcement on uses existing spending and unresolved usage; it does not reset the budget or restart failed stories.</p>
        {query.data.accounting_incomplete && <p role="status" className="mt-2 text-sm text-amber-700 dark:text-amber-300">Some usage could not be metered. Enforcement can stay off; turning it on will block spending until those records are reconciled.</p>}
      </>}
      {mutation.isError && <p role="alert" className="mt-2 text-sm text-red-600">The change was not saved. The latest setting has been reloaded; try again.</p>}
      {mutation.isSuccess && <p role="status" className="mt-2 text-sm">Budget enforcement setting saved.</p>}
    </section>
  );
}
