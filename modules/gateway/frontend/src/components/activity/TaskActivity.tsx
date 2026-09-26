import { useId, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getMyTaskActivity } from '@/services/taskActivity';
import type { InvocationItem } from '@/types/activity';

/** Independent pagination keeps Task discovery separate from native chain ordering. */
export function TaskActivity({ onOpen }: { onOpen: (item: InvocationItem) => void }) {
  const instance = useId();
  const [expanded, setExpanded] = useState(false);
  const [pages, setPages] = useState<(string | undefined)[]>([undefined]);
  const cursor = pages[pages.length - 1];
  const query = useQuery({ queryKey: ['owned-task-activity', instance, cursor], queryFn: () => getMyTaskActivity(cursor),
    enabled: expanded, refetchInterval: expanded ? 30000 : false, retry: false, gcTime: 0 });
  return <section className="bg-white dark:bg-gray-800 rounded-lg p-4" aria-label="My coding Tasks">
    <button type="button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)} className="font-semibold text-blue-600">
      {expanded ? 'Hide Tasks' : 'View my Tasks'}
    </button>
    {expanded && <div className="mt-3 space-y-3">
      <p className="text-sm">Newly submitted Tasks appear here. Older Tasks can still be opened using their invocation ID.</p>
      {query.isPending && <p role="status">Loading Tasks…</p>}
      {query.isError && <p role="alert">Tasks unavailable. Current Task enrollment and read access are required.</p>}
      {!query.isPending && !query.isError && !query.data?.items.length && <p>No Tasks on this page.</p>}
      {!query.isError && query.data?.items.map(item => <div key={item.invocation_id} className="flex gap-3 items-center">
        <span>{item.persona} · {item.task_snapshot?.status ?? item.status}</span>
        <button type="button" className="text-blue-600 underline" onClick={() => onOpen(item)}>View Task stream</button>
      </div>)}
      <div className="flex gap-3">
        <button type="button" disabled={pages.length === 1 || query.isFetching} onClick={() => setPages(value => value.slice(0, -1))}>Previous Tasks</button>
        <button type="button" disabled={!query.data?.last_key || query.isFetching || query.isError}
          onClick={() => setPages(value => [...value, query.data!.last_key!])}>Next Tasks</button>
      </div>
    </div>}
  </section>;
}
