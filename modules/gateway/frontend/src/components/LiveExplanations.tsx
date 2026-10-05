import './run-workspace.css';
import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { useRevalidatingFeaturesQuery } from '@/hooks/useFeatures';
import { FeedError, readExplanations, type LiveExplanation } from '@/services/agentExplanations';

/** Authored explanations remain separate from control acknowledgements. */
export function LiveExplanations({ invocationId, isOpen, terminal, onTerminal, workspace = false }: {
  invocationId: string; isOpen: boolean; terminal: boolean; onTerminal?: () => void; workspace?: boolean;
}) {
  const [filter, setFilter] = useState<'all' | 'updates' | 'tools'>('all');
  const flags = useRevalidatingFeaturesQuery();
  const enabled = !flags.isPending && !flags.isError && flags.data?.agent_explanations === true;
  const [events, setEvents] = useState<LiveExplanation[]>([]);
  const [plan, setPlan] = useState<LiveExplanation>();
  const [status, setStatus] = useState('Connecting…');
  const [now, setNow] = useState(Date.now());
  const [gap, setGap] = useState(false);
  const [updates, setUpdates] = useState(0);
  const end = useRef<HTMLDivElement>(null);
  const callback = useRef(onTerminal); callback.current = onTerminal;
  useEffect(() => { setEvents([]); setPlan(undefined); }, [invocationId]);
  useEffect(() => {
    if (!enabled || !isOpen || terminal || status === 'Finished') return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [enabled, isOpen, terminal, status]);
  useEffect(() => {
    if (!enabled || !isOpen || terminal) return;
    let closed = false, finished = false, cursor: string | undefined, generation: number | undefined, sequence = 0;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let watchdog: ReturnType<typeof setInterval> | undefined;
    let controller: AbortController;
    let backoff = 1000, lastReceived = Date.now();
    let history: LiveExplanation[] = [];
    setEvents([]); setPlan(undefined); setGap(false); setUpdates(0);
    async function connect() {
      controller = new AbortController(); lastReceived = Date.now();
      setStatus(cursor ? 'Reconnecting…' : 'Connecting…');
      watchdog = setInterval(() => {
        if (Date.now() - lastReceived > 10000) { setStatus('Connection stale; reconnecting…'); controller.abort(); }
      }, 2000);
      try {
        await readExplanations(invocationId, cursor, controller.signal, update => {
          if (closed) return;
          lastReceived = Date.now();
          if (update.kind === 'finished') { finished = true; setStatus('Finished'); callback.current?.(); controller.abort(); return; }
          if (update.kind === 'unavailable') { setStatus('Live feed unavailable'); return; }
          setStatus('Connected'); backoff = 1000;
          if (update.kind === 'reset') { setGap(true); sequence = 0; cursor = undefined; history = []; setEvents([]); setPlan(undefined); }
          const event = update.event;
          if (!event) return; // A heartbeat is connectivity, never explanation progress.
          if (generation !== undefined && generation !== event.generation) { setGap(true); sequence = 0; history = []; setEvents([]); setPlan(undefined); }
          generation = event.generation;
          if (event.sequence <= sequence) return;
          if (sequence && event.sequence !== sequence + 1) setGap(true);
          sequence = event.sequence; cursor = update.cursor;
          if (event.kind === 'terminal') { finished = true; setStatus('Finished'); callback.current?.(); return; }
          if (event.payload.progress?.category === 'plan') setPlan(event);
          const id = event.payload.progress?.id;
          const previous = id ? history.findIndex(item => item.payload.progress?.id === id) : -1;
          if (previous >= 0) history[previous] = event;
          else history.push(event);
          let bytes = history.reduce((sum, item) => sum + (item.payload.text?.length ?? 0), 0);
          while (history.length > 128 || bytes > 131072) {
            bytes -= history.shift()!.payload.text?.length ?? 0; setGap(true);
          }
          setEvents([...history]);
          setUpdates(n => n + 1);
        });
      } catch (error) {
        if (closed || finished) return;
        if (error instanceof FeedError && error.terminal) {
          finished = true; setStatus('Finished'); callback.current?.();
        } else if (error instanceof FeedError && [401, 403, 404].includes(error.status)) {
          finished = true; setEvents([]); setPlan(undefined); setStatus('Access unavailable. Reopen after signing in.');
        } else setStatus('Disconnected; reconnecting…');
      } finally {
        clearInterval(watchdog);
        if (!closed && !finished) { retry = setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 30000); }
      }
    }
    void connect();
    return () => { closed = true; clearTimeout(retry); clearInterval(watchdog); controller?.abort(); };
  }, [enabled, invocationId, isOpen, terminal]);
  if (!isOpen) return null;
  if (!enabled) return workspace ? <p className="text-sm">Live explanations are unavailable. Run metadata and any retained transcript remain available.</p> : null;
  const last = events.reduce<LiveExplanation | undefined>((latest, event) => !latest || event.sequence > latest.sequence ? event : latest, undefined);
  return <section aria-label="Implementation explanations" className="space-y-3 border-t pt-4">
    <h3 className="font-semibold">Implementation explanations</h3>
    <p role="status" aria-live="polite" className="text-sm text-gray-500">{terminal ? 'Finished — final transcript available below.' : status}</p>
    {last && <p className="text-xs text-gray-500">Last explanation: <time dateTime={last.timestamp}>{new Date(last.timestamp).toLocaleTimeString()}</time></p>}
    {gap && <p role="status">Some live history is unavailable. The final transcript may contain more detail.</p>}
    {!events.length && !terminal && <p className="text-sm">Waiting for an explanation from this run.</p>}
    <div className={workspace ? "run-progress-grid" : "space-y-4"}>
    <aside className="run-checklist">
    {plan && <section aria-label="Task checklist" className="rounded border p-3 prose prose-sm dark:prose-invert max-w-none">
      <h4>Task checklist</h4>
      <ReactMarkdown skipHtml components={{ img: () => null }}>{plan.payload.text}</ReactMarkdown>
    </section>}
    {!plan && <p className="text-sm text-gray-500">{terminal ? 'Assignment checklist was not captured in this live view. Check the retained run record.' : 'Waiting for the assignment checklist.'}</p>}
    <p className="text-xs text-gray-500 mt-3">Agent-reported tasks. Checked items do not prove review acceptance or merge.</p>
    </aside>
    <div className="min-w-0">
    {workspace && <nav aria-label="Activity filters" className="run-tabs">
      {(['all', 'updates', 'tools'] as const).map(value => <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>{value === 'all' ? 'All activity' : value === 'updates' ? 'Updates' : 'Tools & logs'}</button>)}
    </nav>}
    {!!updates && <button type="button" className="text-sm text-blue-600 underline" onClick={() => {
      end.current?.scrollIntoView({ block: 'nearest' }); setUpdates(0);
    }}>Jump to latest ({updates} new updates)</button>}
    <div className="max-h-96 overflow-y-auto space-y-4 break-words" tabIndex={0} aria-label="Explanation history">
      {events.filter(event => event.payload.progress?.category !== 'plan' && (!workspace || filter === 'all' || (filter === 'tools' ? event.payload.progress?.category === 'tool' : event.payload.progress?.category !== 'tool'))).map(event => <article key={`${event.generation}:${event.payload.progress?.id ?? event.sequence}`} className="prose prose-sm dark:prose-invert max-w-none">
        {event.payload.progress?.category === 'tool' && event.payload.progress.state === 'running' && !terminal && status !== 'Finished' &&
          <p className="text-xs text-gray-500">{status === 'Connected' ? 'Running' : 'Last reported running'} · {Math.max(0, Math.floor((now - Date.parse(event.payload.progress.started_at)) / 1000))}s elapsed</p>}
        <ReactMarkdown skipHtml components={{ img: () => null }}>{event.payload.text}</ReactMarkdown>
      </article>)}
      <div ref={end} />
    </div>
    </div>
    </div>
  </section>;
}
