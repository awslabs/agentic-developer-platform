import { useEffect, useRef, useState } from 'react';
import { readTaskEvents, type TaskEvent } from '@/services/taskActivity';

/** Durable Task events are bounded evidence, not a native client transcript. */
export function TaskEventStream({ taskId, isOpen, onTerminal }: {
  taskId: string; isOpen: boolean; onTerminal?: () => void;
}) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [status, setStatus] = useState('Connecting…');
  const [gap, setGap] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const cursor = useRef<string | undefined>(undefined);
  const sequence = useRef(0);
  const history = useRef<TaskEvent[]>([]);
  const callback = useRef(onTerminal); callback.current = onTerminal;
  useEffect(() => { cursor.current = undefined; sequence.current = 0; history.current = []; setEvents([]); setGap(false); }, [taskId]);
  useEffect(() => {
    if (!isOpen) return;
    const controller = new AbortController();
    let terminal = false;
    setStatus('Connecting…');
    void readTaskEvents(taskId, cursor.current, controller.signal, update => {
      if (controller.signal.aborted) return;
      if (update.kind === 'snapshot') { setStatus(`Connected; Task ${update.status ?? 'status unknown'}`); return; }
      const event = update.event!;
      if (event.sequence <= sequence.current) return;
      if ((event.sequence !== sequence.current + 1) || event.type === 'history.gap') setGap(true);
      sequence.current = event.sequence; cursor.current = update.cursor;
      const next = [...history.current, event];
      let size = next.reduce((total, item) => total + JSON.stringify(item).length, 0);
      while (next.length > 128 || size > 262144) { size -= JSON.stringify(next.shift()).length; setGap(true); }
      history.current = next; setEvents(next);
      terminal = ['task.completed', 'task.failed', 'task.cancelled'].includes(event.type);
      setStatus(terminal ? 'Task finished' : 'Connected');
      if (terminal) callback.current?.();
    }).then(() => {
      if (!controller.signal.aborted && !terminal) setStatus('Stream disconnected. Reconnect to resume retained events.');
    }).catch(() => {
      if (!controller.signal.aborted) setStatus('Task stream unavailable. Access may have changed or retained history may have expired.');
    });
    return () => controller.abort();
  }, [taskId, isOpen, attempt]);
  if (!isOpen) return null;
  return <section aria-label="Task event stream" className="mt-4 space-y-2">
    <h3 className="font-semibold">Task event stream</h3>
    <p className="text-sm">Retained Task events; this is not a full native agent transcript.</p>
    <p role="status">{status}</p>
    {gap && <p role="status">Some event history is omitted from this view.</p>}
    <button type="button" className="text-blue-600 underline" onClick={() => setAttempt(value => value + 1)}>Reconnect stream</button>
    <div className="max-h-96 overflow-auto" tabIndex={0} aria-label="Task events">
      {events.map(event => <article key={event.sequence} className="py-2">
        <p>{event.type} · {event.timestamp}</p>
        <pre className="whitespace-pre-wrap break-words text-sm">{JSON.stringify(event.data, null, 2)}</pre>
      </article>)}
    </div>
  </section>;
}
