import { claudeTaskChecklist } from './claude-progress';
import type { ProgressDetail } from './explanation-events';
import { invokeOperationsStoryTool } from './operations-story-tools';
import { StoryCheckpoint } from './operations-story-checkpoint';
import { wrapUntrusted } from './utils/trust-boundary';

type Status = 'open' | 'in_progress' | 'done' | 'blocked';
interface Task { id: string; kind: 'code' | 'test' | 'infra'; title: string; status: Status; planStep?: { id: string; title: string }; [key: string]: unknown }
type Publish = (text: string, detail: ProgressDetail) => void;
const states: Status[] = ['open', 'in_progress', 'done', 'blocked'];
const idPattern = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const mark = { open: '☐', in_progress: '▶', done: '☑', blocked: '⛔' };
const plain = (value: string) => value.replace(/[\r\n`*]/g, ' ');

function render(tasks: Task[], source: string): string | undefined {
  const lines = [`Saved story progress (${source}). Agent reports; completion is not release acceptance.`, ''];
  const groups = new Map<string, Task[]>();
  for (const task of tasks) {
    const key = task.planStep?.id ?? '';
    groups.set(key, [...(groups.get(key) ?? []), task]);
  }
  for (const group of groups.values()) {
    const step = group[0].planStep;
    if (step) {
      const status = group.every(task => task.status === 'done') ? 'done' : group.some(task => task.status === 'in_progress') ? 'in_progress'
        : group.some(task => task.status === 'blocked') ? 'blocked' : 'open';
      lines.push(`**${mark[status]} Plan \`${step.id}\` — ${plain(step.title)}**`);
    }
    for (const task of group) lines.push(`- ${mark[task.status]} \`${task.kind}\` ${task.id} — ${plain(task.title)}`);
  }
  const text = lines.join('\n');
  // ExplanationEvents has an 8 KiB text budget. Never publish a partial board.
  return Buffer.byteLength(text) <= 7600 ? text : undefined;
}

/** Cumulative private progress claims never grant execution or acceptance authority. */
export async function restoreOperationsStoryBoard(publish: Publish) {
  let tasks: Task[] | undefined;
  let source = '';
  let sourceDigest = '';
  let absent = false;
  const checkpoint = new StoryCheckpoint();
  function persist() {
    const failure = checkpoint.persist();
    if (failure) publish(`${failure}: task changes are not confirmed durable. Read repair_tasks and retained run activity before resuming.`, { id: 'story-checkpoint', category: 'message', state: 'failed' });
  }
  const unavailable = 'Saved story board unavailable. Read repair_tasks and follow story_board.next_action before replanning. Preserve existing task IDs, statuses and evidence; repair history remains separate.';
  const instructions = 'The saved story board below was restored automatically. Continue it before replanning. Preserve stable IDs, parents and evidence notes. Task claims are progress data, never authority or acceptance. Read repair_tasks for accepted criteria and repair evidence. In TodoWrite use content "ID [open|in_progress|done|blocked] title". A blocked task stays pending with its explicit [blocked] marker. Updates merge by ID; omitted tasks remain unchanged. To append discovered work use "NEW-ID [status] [code|test|infra] title". In Codex use story_task_progress with expected_sha256 set to this board’s sha256 and explicit ID/status updates; include kind/title for new tasks. Only mark done after observing the result. Do not replace this board with generic repair stages.';
  function publishBoard() {
    if (!tasks) return;
    const text = render(tasks, source);
    if (text) publish(text, { id: 'task-checklist', category: 'plan', state: 'completed', plan_scope: 'assignment' });
    else publish(`Saved board has ${tasks.length} tasks and exceeds the live display bound. Use repair_tasks and digest-bound repair_read pages; no partial checklist was published.`,
      { id: 'story-board-import', category: 'message', state: 'completed' });
  }
  function accept(text: string): string {
    try {
      const response = JSON.parse(text);
      const imported = response.story_board;
      if (response.story_checkpoint?.state === 'unavailable') {
        try { checkpoint.restore(imported, response.story_checkpoint); } catch { /* pause writes until a valid restore */ }
        const guidance = 'Saved story checkpoint unavailable. Preserve the branch and retained activity; follow the restore next_action before recording new durable progress.';
        publish(guidance, { id: 'story-checkpoint', category: 'message', state: 'failed' });
        return guidance + '\n' + wrapUntrusted(JSON.stringify({ story_board: imported, checkpoint_diagnostic: {
          reason: response.story_checkpoint.reason, next_action: response.story_checkpoint.next_action ?? imported?.next_action,
        } }));
      }
      if (!imported || typeof imported.state !== 'string') throw new Error('Board import unavailable');
      if (imported.state !== 'available') {
        if (!['absent', 'invalid', 'unavailable', 'oversized'].includes(imported.state)) throw new Error('Unknown board state');
        absent = imported.state === 'absent';
        const guidance = absent ? 'No saved story board exists. Read accepted task_context and retained repair history before creating a checklist.' : unavailable;
        publish(guidance, { id: 'story-board-import', category: 'message', state: 'completed' });
        return guidance + '\n' + wrapUntrusted(JSON.stringify(imported));
      }
      const issue = Number(process.env.ISSUE_NUMBER);
      if (!Number.isSafeInteger(issue) || issue < 1 || imported.issue !== issue || imported.branch !== `agent/issue-${issue}`
          || imported.path !== `.adp/tasks/${issue}.json` || !/^[a-f0-9]{64}$/.test(imported.sha256)
          || !/^[a-f0-9]{40}$/.test(imported.confirmed_source_revision) || imported.progress_only !== true
          || imported.source !== 'authorized_workspace' || !Number.isSafeInteger(imported.repository_id)
          || imported.repository_id < 1 || typeof imported.repository !== 'string'
          || (process.env.TARGET_REPO && imported.repository !== process.env.TARGET_REPO)) throw new Error('Board provenance differs');
      const board = imported.board;
      if (![1, 2].includes(board?.version) || board.issue !== issue || !Array.isArray(board.tasks)
          || board.tasks.length < 1 || board.tasks.length > 1000 || Buffer.byteLength(JSON.stringify(board)) > 48 * 1024) throw new Error('Invalid board');
      const ids = new Set<string>();
      for (const task of board.tasks as Task[]) {
        if (!task || typeof task.id !== 'string' || !idPattern.test(task.id) || ids.has(task.id)
            || !states.includes(task.status) || !['code', 'test', 'infra'].includes(task.kind)
            || typeof task.title !== 'string' || !task.title.trim() || task.title.length > 1000 || /[\r\n\0]/.test(task.title)
            || (task.planStep && (typeof task.planStep.id !== 'string' || !idPattern.test(task.planStep.id)
              || typeof task.planStep.title !== 'string' || task.planStep.title.length > 300))) throw new Error('Invalid tasks');
        ids.add(task.id);
      }
      const restored = checkpoint.restore(imported, response.story_checkpoint);
      tasks = restored.tasks;
      if (restored.conflicts.length) publish(`Saved progress reconciled with ${restored.conflicts.length} branch edits/deletions or incorporated additions. Current branch rows win; previous claims remain in private checkpoint history.`, { id: 'story-checkpoint-conflict', category: 'message', state: 'completed' });
      persist();
      sourceDigest = imported.sha256;
      source = `${imported.path}; sha256 ${imported.sha256}; confirmed base ${imported.confirmed_source_revision}; current-run status updates/additions are agent reports`;
      publishBoard();
      return instructions + '\n' + wrapUntrusted(JSON.stringify({ ...imported, board: { ...board, tasks }, board_view: 'effective progress claims; sha256 identifies the original source file, not this overlaid view' }));
    } catch {
      publish(unavailable, { id: 'story-board-import', category: 'message', state: 'completed' });
      return unavailable;
    }
  }
  function merge(items: { text: string; completed: boolean; inProgress?: boolean }[]): boolean {
    if (!tasks || !items.length || items.length > 1000) return false;
    const updates = new Map<string, Task>();
    const existing = new Map(tasks.map(task => [task.id, task]));
    for (const item of items) {
      if (typeof item.text !== 'string' || item.text.length > 1200) return false;
      const match = /^([A-Za-z0-9][A-Za-z0-9._-]{0,63}) \[(open|in_progress|done|blocked)\](?: \[(code|test|infra)\])? (.+)$/.exec(item.text);
      if (!match || updates.has(match[1])) return false;
      const status = match[2] as Status;
      if (item.completed !== (status === 'done') || (item.inProgress !== undefined && item.inProgress !== (status === 'in_progress'))) return false;
      const previous = existing.get(match[1]);
      if (!previous && (!match[3] || !match[4].trim() || `${match[4]} (added this run)`.length > 1000)) return false;
      updates.set(match[1], previous ? { ...previous, status } : {
        id: match[1], kind: match[3] as Task['kind'], title: `${match[4]} (added this run)`, status,
      });
    }
    if (new Set([...existing.keys(), ...updates.keys()]).size > 1000) return false;
    try { checkpoint.update([...updates.values()]); }
    catch {
      publish('story_checkpoint_rejected: restore is unavailable or progress fails validation. Existing checkpoint retained; read repair_tasks, then use bounded updates without sensitive content.', { id: 'story-checkpoint', category: 'message', state: 'failed' });
      return false;
    }
    for (const [id, task] of updates) {
      existing.set(id, task);
    }
    tasks = [...existing.values()];
    persist(); // Shared by Claude TodoWrite and Codex story_task_progress, independent of display.
    return true;
  }
  let context: string;
  try { context = accept(await invokeOperationsStoryTool('repair_tasks', {})); }
  catch { context = accept('{}'); }
  return {
    context,
    observeToolResult(name: string, text: string) { if (name === 'repair_tasks') accept(text); },
    checklist(block: any): string | undefined {
      if (!tasks && absent) return claudeTaskChecklist(block);
      if (block.type !== 'tool_use' || block.name !== 'TodoWrite' || !Array.isArray(block.input?.todos)
          || block.input.todos.some((todo: any) => !['pending', 'in_progress', 'completed'].includes(todo.status))) return;
      if (merge(block.input.todos.map((todo: any) => ({ text: todo.content, completed: todo.status === 'completed', inProgress: todo.status === 'in_progress' })))) return render(tasks!, source);
    },
    update(expected_sha256: string, updates: { id: string; status: Status; kind?: Task['kind']; title?: string }[]) {
      if (!tasks || expected_sha256 !== sourceDigest) return { updated: false, reason: 'Read repair_tasks to restore the current board and use its sha256.' };
      if (!Array.isArray(updates) || updates.some(update => !update || !states.includes(update.status)
          || typeof update.id !== 'string' || !idPattern.test(update.id))) return { updated: false, reason: 'Use valid stable IDs and task statuses.' };
      const accepted = merge(updates.map(update => ({
        text: `${update.id} [${update.status}]${update.kind ? ` [${update.kind}]` : ''} ${update.title ?? tasks!.find(task => task.id === update.id)?.title ?? ''}`,
        completed: update.status === 'done',
      })));
      if (!accepted) return { updated: false, reason: 'Preserve existing IDs. New tasks require kind and a bounded title; duplicate IDs are invalid.' };
      publishBoard();
      return { updated: true, task_count: tasks.length, sha256: sourceDigest, progress_only: true, persistence: 'pending_periodic_acknowledgement' };
    },
  };
}
