import type { LiveExplanation } from '@/services/agentExplanations';

export interface LiveTask {
  id: string;
  kind: 'code' | 'test' | 'infra';
  status: 'pending' | 'in_progress' | 'completed' | 'blocked';
  group: string;
  description: string;
  planStep?: { id: string; title: string; status?: LiveTask['status'] };
}

/** Recognize controller-authored board rows. Fall back to the original Markdown
 * if any task row is unfamiliar or ambiguous; never display partial totals. */
export function parseLiveTasks(markdown: string): LiveTask[] | undefined {
  const tasks: LiveTask[] = [];
  let group = '';
  let planStep: LiveTask['planStep'];
  let fenced = false;
  for (const line of markdown.split('\n')) {
    if (/^\s*(`{3,}|~{3,})/.test(line)) { fenced = !fenced; continue; }
    if (fenced) continue;
    const plan = /^\*\*([☑☐▶⛔]) Plan `([A-Za-z0-9][A-Za-z0-9._-]{0,63})` — (.{1,300})\*\*$/.exec(line.trim());
    if (plan) {
      planStep = { id: plan[2], title: plan[3], status: plan[1] === '☑' ? 'completed' : plan[1] === '▶' ? 'in_progress' : plan[1] === '⛔' ? 'blocked' : 'pending' };
      group = planStep.title;
      continue;
    }
    const heading = /^\*\*([^*]+)\*\*$/.exec(line.trim());
    if (heading) { group = heading[1]; planStep = undefined; }
    if (!/^\s*(?:[-*+]|\d+[.)])\s+/.test(line)) continue;
    const row = /^- ([☑☐▶⛔]) `(code|test|infra)` ([A-Za-z0-9][A-Za-z0-9._-]{0,63}) — (.+)$/.exec(line);
    if (!row || tasks.some(task => task.id === row[3])) return;
    tasks.push({ id: row[3], kind: row[2] as LiveTask['kind'], group, description: row[4], ...(planStep ? { planStep } : {}),
      status: row[1] === '☑' ? 'completed' : row[1] === '▶' ? 'in_progress' : row[1] === '⛔' ? 'blocked' : 'pending' });
  }
  return tasks.length ? tasks : undefined;
}

export function implementationSteps(tasks: LiveTask[]) {
  const groups = new Map<string, { id: string; title: string; tasks: LiveTask[]; reported?: LiveTask['status'] }>();
  for (const task of tasks) {
    const id = task.planStep?.id ?? `legacy:${task.group}`;
    const group = groups.get(id) ?? { id, title: task.planStep?.title ?? (task.group ? `Earlier tasks: ${task.group}` : 'Earlier tasks'), tasks: [], reported: task.planStep?.status };
    group.tasks.push(task); groups.set(id, group);
  }
  return [...groups.values()].map(group => {
    const completed = group.tasks.filter(task => task.status === 'completed').length;
    const status: LiveTask['status'] = completed === group.tasks.length && (!group.reported || group.reported === 'completed') ? 'completed'
      : group.tasks.some(task => task.status === 'in_progress') ? 'in_progress'
        : group.tasks.some(task => task.status === 'blocked') ? 'blocked' : completed ? 'in_progress' : 'pending';
    return { ...group, completed, status };
  });
}

/** This is a textual reference, not proof of task ownership or completion.
 * Exclude plan snapshots so the board itself never counts as task activity. */
export function mentionsTask(event: LiveExplanation, id: string): boolean {
  if (event.payload.progress?.category === 'plan') return false;
  const escaped = id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`(^|[^A-Za-z0-9._-])${escaped}(?![A-Za-z0-9_-]|\\.[A-Za-z0-9])`).test(event.payload.text ?? '');
}
