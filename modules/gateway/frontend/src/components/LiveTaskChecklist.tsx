import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { implementationSteps, type LiveTask } from '@/utils/liveTasks';

const kinds = { code: 'Implementation', test: 'Validation', infra: 'Setup' };
const priority = { in_progress: 0, blocked: 1, pending: 2, completed: 3 };
const statuses = { pending: 'Not started', in_progress: 'In progress', completed: 'Completed', blocked: 'Blocked / deferred' };

export function LiveTaskChecklist({ tasks, selected, counts, live, onSelect }: {
  tasks: LiveTask[]; selected?: string; counts: Map<string, number>; live: boolean; onSelect?: (id: string) => void;
}) {
  const [filter, setFilter] = useState('all');
  const [expanded, setExpanded] = useState<string[]>([]);
  const selectedParent = tasks.find(task => task.id === selected)?.planStep?.id;
  useEffect(() => {
    if (selectedParent) setExpanded(previous => previous.includes(selectedParent) ? previous : [...previous, selectedParent]);
  }, [selected, selectedParent]);
  const complete = tasks.filter(task => task.status === 'completed').length;
  const working = tasks.filter(task => task.status === 'in_progress');
  const blocked = tasks.filter(task => task.status === 'blocked').length;
  const pending = tasks.filter(task => task.status === 'pending').length;
  const visible = tasks.filter(task => filter === 'all' || (filter === 'remaining' ? task.status !== 'completed' : task.status === filter)).sort((a, b) => priority[a.status] - priority[b.status]);
  const hierarchical = tasks.some(task => task.planStep);
  const steps = implementationSteps(tasks);
  const row = (task: LiveTask) => <li key={task.id} tabIndex={-1} className="task-row" data-status={task.status} data-selected={selected === task.id}
    aria-current={task.status === 'in_progress' && live ? 'step' : undefined}>
    <span className="task-status">{task.status === 'in_progress' && !live ? 'Last reported in progress' : statuses[task.status]}</span>
    <div className="live-markdown task-description"><ReactMarkdown skipHtml components={{ img: () => null }}>{task.description}</ReactMarkdown></div>
    <p className="live-muted task-reference">{kinds[task.kind]} · <code>{task.id}</code>{!hierarchical && task.group && task.group !== 'general' ? ` · ${task.group}` : ''}</p>
    {onSelect && <button type="button" className="task-activity-link" aria-label={`View activity mentioning ${task.id}`} aria-pressed={selected === task.id}
      onClick={() => onSelect(task.id)}>View activity ({counts.get(task.id) ?? 0})</button>}
  </li>;
  return <div className="task-board">
    {hierarchical && <p className="task-count">{steps.filter(step => step.status === 'completed').length} of {steps.length} steps completed</p>}
    <p className={hierarchical ? 'task-count-detail' : 'task-count'}>{complete} of {tasks.length} {hierarchical ? 'detailed tasks' : 'tasks'} completed</p>
    <progress aria-label="Reported task completion" value={hierarchical ? steps.filter(step => step.status === 'completed').length : complete} max={hierarchical ? steps.length : tasks.length} />
    <p className="live-muted task-count-detail">{working.length} in progress · {pending} not started · {blocked} blocked / deferred</p>
    {!!working.length && <div className="task-current"><strong>{live ? 'Working on' : 'Last reported in progress'}</strong>
      {(hierarchical ? [...new Set(working.map(task => task.planStep?.title ?? task.description))] : working.map(task => task.description)).map(title => <p key={title}>{title}</p>)}
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
      {hierarchical ? steps.filter(step => step.tasks.some(task => visible.includes(task))).map(step => <li key={step.id} className="plan-step" data-status={step.status}>
        <details open={expanded.includes(step.id)} onToggle={event => {
          const open = event.currentTarget.open;
          setExpanded(previous => open ? previous.includes(step.id) ? previous : [...previous, step.id] : previous.filter(id => id !== step.id));
        }}>
          <summary><span className="plan-step-title">{step.title}</span><span className="task-status">{step.status === 'in_progress' && !live ? 'Last reported in progress' : statuses[step.status]}</span>
            <span className="task-count-detail">{step.completed} of {step.tasks.length} detailed tasks completed</span></summary>
          <ul aria-label={`Detailed tasks for ${step.title}`}>{step.tasks.filter(task => visible.includes(task)).map(row)}</ul>
        </details>
      </li>) : visible.map(row)}
    </ul>
    {!visible.length && <p className="live-muted">No tasks in this category.</p>}
  </div>;
}
