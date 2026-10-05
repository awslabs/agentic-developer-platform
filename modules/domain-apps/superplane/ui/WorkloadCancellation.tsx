import { useEffect, useRef, useState } from 'react';
import { Alert, Button } from '@/components/ui';
import { isSuperseded, ScopeGuard } from './client';
import type { Unavailable } from './contract';
import { cancelWorkload, type ServingDeployment, type WorkloadKind } from './workloads';

interface Props { workspaceId: string; row: ServingDeployment; kind: WorkloadKind; onProgress: () => void }

export function WorkloadCancellation(props: Props) {
  return <Cancellation key={`${props.workspaceId}:${props.row.operationId}`} {...props} />;
}

function Cancellation({ workspaceId, row, kind, onProgress }: Props) {
  const [guard] = useState(() => new ScopeGuard());
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => () => guard.supersede(), [guard]);
  async function cancel() {
    if (inFlight.current || !row.deploymentId || !row.operationId) return;
    inFlight.current = true; setBusy(true);
    const result = await cancelWorkload(guard, workspaceId, row.deploymentId, row.operationId, kind);
    if (isSuperseded(result)) return;
    inFlight.current = false; setBusy(false);
    if (!result.ok) {
      if ('unavailable' in result) setProblem(result.unavailable);
      setMessage(null); return;
    }
    setProblem(null);
    setMessage(result.value.cleanup === 'not-required'
      ? 'Cancelled before dispatch. The backend confirmed no workload was started.'
      : result.value.requested
        ? 'Cancellation requested. Resource cleanup remains subject to backend reconciliation.'
        : 'The original operation has already settled. Refresh its status and review stop if cleanup is needed.');
    onProgress();
  }
  return <div className="mt-2 space-y-2">
    <Button variant="secondary" disabled={busy || row.cancellationRequested || problem?.reason === 'not-permitted'} onClick={() => void cancel()}>
      {busy ? 'Requesting cancellation…' : `Cancel pending operation for ${row.name}`}
    </Button>
    {message && <p role="status">{message}</p>}
    {problem && <Alert variant="warning" title="Cancellation outcome unavailable">{problem.detail} Retry or refresh using the same original operation reference.</Alert>}
  </div>;
}
