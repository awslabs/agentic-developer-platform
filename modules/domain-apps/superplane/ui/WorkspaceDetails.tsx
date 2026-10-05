import { useEffect, useState } from 'react';

import { Alert, Button } from '@/components/ui';
import { getAccessToken } from '@/services/auth';

import { getOperation, getWorkspace, isSuperseded, ScopeGuard } from './client';
import type { OperationReceipt, WorkspaceSummary } from './contract';
import type { ReceiptScope } from './operations';
import { freshnessOf } from './readiness';

type DetailsState =
  | { phase: 'loading' }
  | { phase: 'failed'; detail: string }
  | {
      phase: 'loaded';
      workspace: WorkspaceSummary;
      operation: OperationReceipt | null;
      operationProblem: string | null;
      checkingOperation: boolean;
    };

export function WorkspaceDetails({ workspaceId, scope, guard, sessionToken, now }: {
  workspaceId: string;
  scope: ReceiptScope;
  guard: ScopeGuard;
  sessionToken: string;
  now: number;
}) {
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<DetailsState>({ phase: 'loading' });
  const clusterFreshness = state.phase === 'loaded' ? freshnessOf(state.workspace.last_heartbeat, now) : 'unknown';

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      setState({ phase: 'loading' });
      if (getAccessToken() !== sessionToken) {
        setState({ phase: 'failed', detail: 'Your session changed. Sign in and select this workspace again.' });
        return;
      }
      const detail = await getWorkspace(guard, workspaceId);
      if (!active || isSuperseded(detail) || getAccessToken() !== sessionToken) return;
      if (!detail.ok) {
        if ('unavailable' in detail) setState({
          phase: 'failed', detail: detail.unavailable.reason === 'not-permitted'
            ? 'Workspace details are not permitted for this session. Ask an administrator for workspace access; organization access alone is not enough.'
            : detail.unavailable.detail,
        });
        return;
      }
      const workspace = detail.value;
      if (workspace.id !== workspaceId || workspace.org_id !== scope.orgId) {
        setState({ phase: 'failed', detail: 'The workspace response did not match the selected organization and workspace.' });
        return;
      }

      const operationId = workspace.provisioning_operation_id;
      setState({ phase: 'loaded', workspace, operation: null, operationProblem: null, checkingOperation: Boolean(operationId) });
      if (!operationId) return;

      if (getAccessToken() !== sessionToken) {
        setState({ phase: 'failed', detail: 'Your session changed. Sign in and select this workspace again.' });
        return;
      }

      const outcome = await getOperation(guard, operationId);
      if (!active || isSuperseded(outcome) || getAccessToken() !== sessionToken) return;
      if (!outcome.ok) {
        if ('unavailable' in outcome) {
          setState({ phase: 'loaded', workspace, operation: null, operationProblem: outcome.unavailable.detail, checkingOperation: false });
        }
        return;
      }
      const operation = outcome.value;
      if ((operation.workspaceId && operation.workspaceId !== workspaceId) ||
          (operation.operationId && operation.operationId !== operationId)) {
        setState({ phase: 'loaded', workspace, operation: null, operationProblem: 'The operation response did not match the selected workspace. The original request was not changed.', checkingOperation: false });
        return;
      }
      setState({ phase: 'loaded', workspace, operation, operationProblem: null, checkingOperation: false });
    };
    void refresh();
    return () => { active = false; };
  }, [guard, workspaceId, scope.orgId, sessionToken, revision]);

  return (
    <section className="mt-6 rounded-lg border border-gray-200 p-4 dark:border-gray-700" aria-label="Workspace details">
      <h3 className="font-semibold">Workspace details</h3>
      {state.phase === 'loading' && <p role="status">Loading workspace details…</p>}
      {state.phase === 'failed' && <Alert variant="error" title="Workspace details unavailable">{state.detail}</Alert>}
      {state.phase === 'loaded' && (
        <>
          <dl className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
            <div><dt>Workspace ID</dt><dd className="break-all">{state.workspace.id}</dd></div>
            <div><dt>Isolation</dt><dd>{state.workspace.isolation_mode || 'Unknown'}</dd></div>
            <div><dt>Registered status</dt><dd>{state.workspace.status}</dd></div>
            <div><dt>Workspace read access</dt><dd>Allowed for this request. Cluster and workload access are not established by reading workspace details.</dd></div>
            <div><dt>Account and region</dt><dd>Not reported by the workspace detail API. Check the original reviewed plan; do not infer them from management location.</dd></div>
            <div><dt>Cluster placement and provider</dt><dd>Not reported by the workspace detail API. Isolation mode does not establish cluster ownership or access.</dd></div>
            <div><dt>Cluster observation</dt><dd>{clusterFreshness === 'fresh' && state.workspace.cluster_health
              ? `Fresh: ${state.workspace.cluster_health}`
              : clusterFreshness === 'stale'
                ? 'Stale; the last reported cluster health does not establish current readiness.'
                : 'Unknown; no usable cluster observation was reported.'}</dd></div>
            <div><dt>Provisioning operation ID</dt><dd className="break-all">{state.workspace.provisioning_operation_id ?? 'Not reported'}</dd></div>
          </dl>
          {state.checkingOperation && <p role="status">Checking the original operation…</p>}
          {state.operation && (
            <div role="status" className="mt-3 text-sm">
              <p>Original request ID: <span className="break-all">{state.operation.idempotencyKey}</span></p>
              <p>Operation phase: {state.operation.phase ?? 'Unknown'}</p>
              <p>Operation state: {state.operation.state}</p>
              <p>Operation completion does not establish workspace readiness. Check the separate readiness readings.</p>
            </div>
          )}
          {state.operationProblem && (
            <Alert variant="warning" title="Operation status unavailable">{state.operationProblem} Keep the original operation identity when recovering.</Alert>
          )}
          {!state.workspace.provisioning_operation_id && (
            <p role="status">This workspace does not report an original provisioning operation. Do not start another request to infer its status.</p>
          )}
          <Button variant="secondary" className="mt-4" onClick={() => setRevision((current) => current + 1)}>
            Refresh workspace details
          </Button>
        </>
      )}
    </section>
  );
}
