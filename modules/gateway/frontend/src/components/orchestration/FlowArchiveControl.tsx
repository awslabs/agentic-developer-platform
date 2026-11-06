import { useState, type FormEvent } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { usePermissions } from '@/hooks/usePermissions';
import { Permission } from '@/types';
import { apiClient } from '@/services/api';
import { Badge, Button } from '@/components/ui';

export function FlowArchiveControl({ flowId, archived, compact = false }: {
  flowId: string;
  archived: boolean;
  compact?: boolean;
}) {
  const { hasPermission } = usePermissions();
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [reason, setReason] = useState('');
  const mutation = useMutation({
    mutationFn: () => apiClient.post(`/orchestration/flows/${encodeURIComponent(flowId)}/${archived ? 'unarchive' : 'archive'}`, { reason: reason.trim() }),
    onSuccess: async () => {
      setEditing(false);
      setReason('');
      await client.invalidateQueries({ queryKey: ['orchestration'] });
    },
  });

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (reason.trim().length >= 8 && reason.trim().length <= 4000) mutation.mutate();
  };
  const failure = mutation.error as { status?: number; detail?: { code?: string; message?: string } } | null;

  return <section aria-label="Flow archive" className="space-y-2">
    <div className="flex flex-wrap items-center justify-between gap-3">
      {archived ? <Badge variant="default">Archived</Badge> : !compact && <span className="text-sm text-gray-700 dark:text-gray-300">Put inactive work away</span>}
      {hasPermission(Permission.PLAN_APPROVE) && <Button variant="secondary" size="sm" disabled={mutation.isPending}
        onClick={() => { mutation.reset(); setEditing(!editing); }}
      >{archived ? 'Unarchive flow' : 'Archive flow'}</Button>}
    </div>
    {editing && <form onSubmit={submit} className="space-y-2">
      <label className="block text-sm text-gray-700 dark:text-gray-300" htmlFor={`archive-reason-${flowId}`}>Reason (8–4000 characters)</label>
      <textarea id={`archive-reason-${flowId}`} required minLength={8} maxLength={4000} value={reason}
        onChange={(event) => setReason(event.target.value)} className="w-full rounded border p-2 dark:bg-gray-800" />
      <Button type="submit" size="sm" disabled={mutation.isPending || reason.trim().length < 8}>
        {mutation.isPending ? 'Saving…' : archived ? 'Confirm unarchive' : 'Confirm archive'}
      </Button>
    </form>}
    {!compact && !archived && <p className="text-sm text-gray-600 dark:text-gray-400">Archiving is not cancellation: finish active work first. The slug remains reserved and history and costs remain readable.</p>}
    {mutation.isError && <p role="alert" className="text-sm text-red-600">
      {failure?.status === 409 && failure.detail?.code === 'flow_has_active_work'
        ? 'This flow still has active work. Resolve running work, pending deployment or held authority before archiving. Its slug remains reserved.'
        : 'The archive change was not saved. Check your access or try again.'}
    </p>}
  </section>;
}
