import './run-workspace.css';
import './live-activity.css';
import remarkGfm from 'remark-gfm';
import { LiveTaskChecklist } from './LiveTaskChecklist';
import { mentionsTask, parseLiveTasks } from '@/utils/liveTasks';
import { Children, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import { useRevalidatingFeaturesQuery } from '@/hooks/useFeatures';
import { FeedError, readExplanations, type LiveExplanation } from '@/services/agentExplanations';

/** The task board marks the task being worked with ▶ (controller-assigned, see
 * codex-reviewer/src/task-board.ts). Markdown cannot animate, so the renderer
 * turns that marker into a pulsing indicator and highlights its row. */
const IN_PROGRESS = '▶';

function animateMarker(children: ReactNode): { children: ReactNode; active: boolean } {
  let active = false;
  const mapped: ReactNode[] = [];
  Children.toArray(children).forEach((child, index) => {
    if (typeof child !== 'string' || !child.includes(IN_PROGRESS)) { mapped.push(child); return; }
    active = true;
    const at = child.indexOf(IN_PROGRESS);
    mapped.push(child.slice(0, at),
      <span key={`marker-${index}`} role="img" aria-label="in progress"
        className="inline-block animate-pulse motion-reduce:animate-none text-blue-600 dark:text-blue-400 font-bold">{IN_PROGRESS}</span>,
      child.slice(at + IN_PROGRESS.length));
  });
  return { children: mapped, active };
}

const markdownComponents: Components = {
  img: () => null,
  pre: ({ children }) => <pre tabIndex={0} aria-label="Code or command output">{children}</pre>,
  table: ({ children }) => <div className="live-table-scroll" tabIndex={0} role="region" aria-label="Activity table"><table>{children}</table></div>,
};

const planComponents: Components = {
  ...markdownComponents,
  li: ({ children }) => {
    const marked = animateMarker(children);
    return <li className={marked.active ? 'rounded bg-blue-50 dark:bg-blue-950/40 px-1 -mx-1' : undefined}
      aria-current={marked.active ? 'step' : undefined}>{marked.children}</li>;
  },
  p: ({ children }) => <p>{animateMarker(children).children}</p>,
};

/** Authored explanations remain separate from control acknowledgements. */
export function LiveExplanations({ invocationId, isOpen, terminal, onTerminal, workspace = false }: {
  invocationId: string; isOpen: boolean; terminal: boolean; onTerminal?: () => void; workspace?: boolean;
}) {
  const [filter, setFilter] = useState<'all' | 'updates' | 'tools'>('all');
  const [selectedTask, setSelectedTask] = useState<string>();
  const historyView = useRef<HTMLDivElement>(null);
  const checklistView = useRef<HTMLElement>(null);
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
  useEffect(() => { setEvents([]); setPlan(undefined); setSelectedTask(undefined); setFilter('all'); setStatus('Connecting…'); }, [invocationId]);
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
    setEvents([]); setPlan(undefined); setSelectedTask(undefined); setGap(false); setUpdates(0);
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
          if (update.kind === 'reset') { setGap(true); sequence = 0; cursor = undefined; history = []; setEvents([]); setPlan(undefined); setSelectedTask(undefined); }
          const event = update.event;
          if (!event) return; // A heartbeat is connectivity, never explanation progress.
          if (generation !== undefined && generation !== event.generation) { setGap(true); sequence = 0; history = []; setEvents([]); setPlan(undefined); setSelectedTask(undefined); }
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
          finished = true; setEvents([]); setPlan(undefined); setSelectedTask(undefined); setStatus('Access unavailable. Reopen after signing in.');
        } else setStatus('Disconnected; reconnecting…');
      } finally {
        clearInterval(watchdog);
        if (!closed && !finished) { retry = setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 30000); }
      }
    }
    void connect();
    return () => { closed = true; clearTimeout(retry); clearInterval(watchdog); controller?.abort(); };
  }, [enabled, invocationId, isOpen, terminal]);
  const tasks = useMemo(() => plan ? parseLiveTasks(plan.payload.text ?? '') : undefined, [plan]);
  const references = useMemo(() => new Map(events.map(event => [event, tasks?.filter(task => mentionsTask(event, task.id)) ?? []])), [events, tasks]);
  useEffect(() => {
    if (selectedTask && !tasks?.some(task => task.id === selectedTask)) setSelectedTask(undefined);
  }, [tasks, selectedTask]);
  const selection = tasks?.find(task => task.id === selectedTask);
  const activity = events.filter(event => event.payload.progress?.category !== 'plan');
  const taskCounts = new Map(tasks?.map(task => [task.id, activity.filter(event => references.get(event)?.some(ref => ref.id === task.id)).length]));
  const filteredEvents = activity.filter(event => (!selection || references.get(event)?.some(task => task.id === selection.id)) &&
    (!workspace || filter === 'all' || (filter === 'tools' ? event.payload.progress?.category === 'tool' : event.payload.progress?.category !== 'tool')));
  const selectTask = (id?: string, destination: 'activity' | 'task' = 'activity') => {
    setSelectedTask(id); setFilter('all');
    // Only a user's navigation moves the viewport. Incoming events never do.
    if (destination === 'task') {
      requestAnimationFrame(() => {
        const row = checklistView.current?.querySelector<HTMLElement>('[data-selected="true"]') ?? checklistView.current;
        row?.focus(); row?.scrollIntoView({ block: 'nearest' });
      });
    } else {
      historyView.current?.focus();
      if (historyView.current) historyView.current.scrollTop = 0;
    }
  };
  if (!isOpen) return null;
  if (!enabled) return workspace ? <p className="text-sm">Live explanations are unavailable. Run metadata and any retained transcript remain available.</p> : null;
  const last = events.reduce<LiveExplanation | undefined>((latest, event) => !latest || event.sequence > latest.sequence ? event : latest, undefined);
  return <section aria-label="Implementation explanations" className="live-activity space-y-3">
    <h3 className="font-semibold">Implementation explanations</h3>
    <p role="status" aria-live="polite" className="live-muted text-sm">{terminal ? 'Finished — final transcript available below.' : status}</p>
    {last && <p className="live-muted text-xs">Last explanation: <time dateTime={last.timestamp}>{new Date(last.timestamp).toLocaleTimeString()}</time></p>}
    {gap && <p role="status">Some live history is unavailable. The final transcript may contain more detail.</p>}
    {!events.length && !terminal && <p className="text-sm">Waiting for an explanation from this run.</p>}
    <div className={workspace ? "run-progress-grid" : "space-y-4"}>
    <aside className="run-checklist">
    {plan && <section ref={checklistView} aria-label="Task checklist" tabIndex={0} className="live-checklist live-markdown">
      <h4>Task checklist</h4>
      <p className="live-muted task-count-detail">Checklist updated <time dateTime={plan.timestamp}>{new Date(plan.timestamp).toLocaleTimeString()}</time></p>
      {tasks ? <>
        <LiveTaskChecklist key={`${invocationId}:${selection?.id ?? ''}`} tasks={tasks} selected={selection?.id} counts={taskCounts}
          live={!terminal && status === 'Connected'} onSelect={selectTask} />
        <details className="task-original"><summary>Original checklist and notes</summary>
          <ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} components={planComponents}>{plan.payload.text}</ReactMarkdown>
        </details>
      </> : <ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} components={planComponents}>{plan.payload.text}</ReactMarkdown>}
    </section>}
    {!plan && <p className="live-muted text-sm">{terminal ? 'Assignment checklist was not captured in this live view. Check the retained run record.' : 'Waiting for the assignment checklist.'}</p>}
    <p className="live-muted text-xs mt-3">Agent-reported tasks. Checked items do not prove review acceptance or merge.</p>
    </aside>
    <div className="min-w-0">
    {workspace && <nav aria-label="Activity filters" className="run-tabs">
      {(['all', 'updates', 'tools'] as const).map(value => <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>{value === 'all' ? 'All activity' : value === 'updates' ? 'Updates' : 'Tools & logs'}</button>)}
    </nav>}
    {tasks && <p className="live-muted task-link-help">Select a task to see updates that mention its ID. Updates without a task reference stay in All activity.</p>}
    {selection && <div className="task-selection" role="status">
      <p>Activity mentioning <strong>{selection.id}</strong></p>
      <p>{selection.description}</p>
      <div className="task-selection-actions"><button type="button" onClick={() => selectTask()}>Show all activity</button>
        <button type="button" onClick={() => selectTask(selection.id, 'task')}>Back to task checklist</button></div>
    </div>}
    {!!updates && <button type="button" className="live-jump" onClick={() => {
      setSelectedTask(undefined); setFilter('all');
      requestAnimationFrame(() => end.current?.scrollIntoView({ block: 'nearest' })); setUpdates(0);
    }}>Jump to latest ({updates} new updates)</button>}
    <div ref={historyView} className="live-history" tabIndex={0} aria-label="Explanation history">
      {filteredEvents.map(event => <article key={`${event.generation}:${event.payload.progress?.id ?? event.sequence}`} className="live-event" data-category={event.payload.progress?.category === 'tool' ? 'tool' : 'message'}>
        <header className="live-event-meta">
          <span>{event.payload.progress?.category === 'tool' ? 'Tool activity' : 'Agent update'}</span>
          <time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleTimeString()}</time>
        </header>
        {!!references.get(event)?.length && <div className="task-mentions" aria-label="Task references">
          <span>Mentions</span>{references.get(event)!.map(task =>
            <button key={task.id} type="button" onClick={() => selectTask(task.id, 'task')} aria-label={`View task ${task.id}`}>{task.id}</button>)}
        </div>}
        {event.payload.progress?.category === 'tool' && event.payload.progress.state === 'running' && !terminal && status !== 'Finished' &&
          <p className="live-muted text-xs">{status === 'Connected' ? 'Running' : 'Last reported running'} · {Math.max(0, Math.floor((now - Date.parse(event.payload.progress.started_at)) / 1000))}s elapsed</p>}
        <div className="live-markdown"><ReactMarkdown skipHtml remarkPlugins={[remarkGfm]} components={markdownComponents}>{event.payload.text}</ReactMarkdown></div>
      </article>)}
      {selection && !filteredEvents.length && <p className="live-muted">{taskCounts.get(selection.id)
        ? 'No updates for this task match the activity filter. Choose All activity above.'
        : 'No retained updates mention this task ID. Other activity may still relate to it; use Show all activity to see the full feed.'}</p>}
      <div ref={end} />
    </div>
    </div>
    </div>
  </section>;
}
