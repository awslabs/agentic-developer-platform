import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { usePermissions } from '@/hooks/usePermissions';
import { Permission } from '@/types';
import type { ExecutionWindow, WindowRenewalRequest } from '@/types/orchestration';
import { apiClient } from '@/services/api';
import { Alert, Button } from '@/components/ui';

type Preview = {
  snapshot: string;
  wall_clock_started_at: string;
  max_wall_clock_seconds: number;
  expires_at: string;
};

export function ExecutionWindowControl({ flowId, window }: { flowId: string; window?: ExecutionWindow | null }) {
  const { hasPermission } = usePermissions();
  const client = useQueryClient();
  const [review, setReview] = useState<{ request: WindowRenewalRequest; preview: Preview } | null>(null);
  const [renewed, setRenewed] = useState(false);
  const preview = useMutation({
    mutationFn: (request: WindowRenewalRequest) => apiClient.post<Preview>(`/orchestration/flows/${encodeURIComponent(flowId)}/window/preview`, request),
    onSuccess: (result, request) => setReview({ request, preview: result }),
  });
  const accept = useMutation({
    mutationFn: (value: NonNullable<typeof review>) => apiClient.post(`/orchestration/flows/${encodeURIComponent(flowId)}/window/accept`, {
      ...value.request, expected_snapshot: value.preview.snapshot,
    }),
    onSuccess: async () => {
      setReview(null);
      setRenewed(true);
      await client.invalidateQueries({ queryKey: ['orchestration'] });
    },
    onError: () => setReview(null),
  });
  if (renewed && window?.status !== 'expired') return <Alert variant="info" title="Execution window renewed">
    Completed work and attempts are preserved. The engine will recheck pending work on its next cycle.
    Other permissions, gates and limits still apply. Renewing the window does not resume a paused flow.
  </Alert>;
  if (!window || window.status === 'active' || window.status === 'complete') return null;
  if (window.status === 'unavailable') return <Alert variant="warning" title="Execution window unavailable">{window.reason}</Alert>;
  const deadline = review ? new Date(Math.min(
    new Date(review.preview.expires_at).getTime(),
    new Date(review.preview.wall_clock_started_at).getTime() + review.preview.max_wall_clock_seconds * 1000,
  )).toLocaleString() : null;
  return <Alert variant="warning" title="Execution window expired">
    <div className="space-y-3">
      <p>New runs are blocked. The deadline was {window.deadline_at ? new Date(window.deadline_at).toLocaleString() : 'unavailable'}.
        {' '}Time spent waiting at gates counts toward this deadline.</p>
      <p>Renewal preserves completed work, attempts and budget settings. It does not grant missing evaluation or deployment permissions.</p>
      {window.renewal_unavailable ? <p>{window.renewal_unavailable}</p> : hasPermission(Permission.PLAN_APPROVE) && window.renewal_request ? <>
        {review ? <>
          <p>New deadline: <strong>{deadline}</strong>. Confirm to renew the window; the engine will recheck remaining blockers.</p>
          <Button size="sm" disabled={accept.isPending} onClick={() => accept.mutate(review)}>{accept.isPending ? 'Renewing…' : 'Confirm renewal'}</Button>
          <Button size="sm" variant="secondary" disabled={accept.isPending} onClick={() => setReview(null)}>Cancel</Button>
        </> : <Button size="sm" variant="secondary" disabled={preview.isPending} onClick={() => preview.mutate(window.renewal_request!)}>
          {preview.isPending ? 'Loading preview…' : 'Review renewal'}
        </Button>}
      </> : <p>The original plan owner with plan approval access can renew this window.</p>}
      {(preview.isError || accept.isError) && <p role="alert">The window was not renewed. The original plan owner must approve it. If the plan changed, reload and review a fresh renewal preview.</p>}
    </div>
  </Alert>;
}
