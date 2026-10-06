import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import type { LiveTask } from '@/utils/liveTasks';

const kinds = { code: 'Implementation', test: 'Validation', infra: 'Setup' };
const priority = { in_progress: 0, blocked: 1, pending: 2, completed: 3 };
const statuses = { pending: 'Not started', in_progress: 'In progress', completed: 'Completed', blocked: 'Blocked / deferred' };

export function LiveTaskChecklist({ tasks, selected, counts, live, onSelect }: {
  tasks: LiveTask[]; selected?: string; counts: Map<string, number>; live: boolean; onSelect: (id: string) => void;
}) {
  const [filter, setFilter] = useState('all');
  const complete = tasks.filter(task => task.status === 'completed').length;
  const working = tasks.filter(task => task.status === 'in_progress');
  const blocked = tasks.filter(task => task.status === 'blocked').length;
  const pending = tasks.filter(task => task.status === 'pending').length;
  const visible = tasks.filter(task => filter === 'all' || (filter === 'remaining' ? task.status !== 'completed' : task.status === filter)).sort((a, b) => priority[a.status] - priority[b.status]);
  return <div className="task-board">
    <p className="task-count">{complete} of {tasks.length} tasks completed</p>
    <progress aria-label="Reported task completion" value={complete} max={tasks.length} />
    <p className="live-muted task-count-detail">{working.length} in progress · {pending} not started · {blocked} blocked / deferred</p>
    {!!working.length && <div className="task-current"><strong>{live ? 'Working on' : 'Last reported in progress'}</strong>
      {working.map(task => <p key={task.id}>{task.description}</p>)}
    </div>}
    <label className="task-filter">Show tasks
      <select value={filter} onChange={event => setFilter(event.target.value)}>
        <option value="all">All ({tasks.length})</option>
        <option value="remaining">Remaining ({tasks.length - complete})</option>
        <option value="blocked">Blocked / deferred ({blocked})</option>
        <option value="completed">Completed ({complete})</option>
      </select>
    </label>
    <ul className="task-list">
      {visible.map(task => <li key={task.id} tabIndex={-1} className="task-row" data-status={task.status} data-selected={selected === task.id}
        aria-current={task.status === 'in_progress' && live ? 'step' : undefined}>
        <span className="task-status">{task.status === 'in_progress' && !live ? 'Last reported in progress' : statuses[task.status]}</span>
        <div className="live-markdown task-description"><ReactMarkdown skipHtml components={{ img: () => null }}>{task.description}</ReactMarkdown></div>
        <p className="live-muted task-reference">{kinds[task.kind]} · <code>{task.id}</code>{task.group && task.group !== 'general' ? ` · ${task.group}` : ''}</p>
        <button type="button" className="task-activity-link" aria-label={`View activity mentioning ${task.id}`} aria-pressed={selected === task.id}
          onClick={() => onSelect(task.id)}>View activity ({counts.get(task.id) ?? 0})</button>
      </li>)}
    </ul>
    {!visible.length && <p className="live-muted">No tasks in this category.</p>}
  </div>;
}
