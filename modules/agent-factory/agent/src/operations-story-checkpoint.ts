/** Private progress claims. Never put this envelope on the public explanation stream. */
import { createHash } from 'node:crypto';
import { renameSync, writeFileSync } from 'node:fs';
import { containsSecret } from './experience-save-hook';

type Status = 'open' | 'in_progress' | 'done' | 'blocked';
export interface StoryTask { id: string; kind: 'code' | 'test' | 'infra'; title: string; status: Status; [key: string]: any }
interface Update { base_task_hash: string; status: Status }
interface Conflict { id: string; reason: 'branch_edited' | 'branch_deleted' | 'addition_incorporated'; claimed_status: Status }
export interface StoryDelta {
  version: 1; progress_only: true; source: { issue: number; repository_id: number; branch: string; path: string; sha256: string; confirmed_source_revision: string };
  updates: Record<string, Update>; additions: Record<string, StoryTask>; conflicts: Conflict[];
  history: { id: string; status: Status; run_id: string; observed_at: string }[];
}
export const CHECKPOINT_BYTES = 64 * 1024;
export function canonical(value: any): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
export const taskHash = (task: StoryTask): string => createHash('sha256').update(canonical(task)).digest('hex');
const states = ['open', 'in_progress', 'done', 'blocked'];
const id = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const compatible = ['tenant_id', 'flow_id', 'node_id', 'issue', 'repository_id', 'installation_id', 'repository', 'branch', 'accepted_plan_version'];
// Match the gateway rejection boundary before any private file is accepted.
const privateSecret = /(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{12,}|\bBearer\s+\S+|(?:password|token|secret|api[_-]?key)["']?\s*[:=]\s*["']?[^\s,;"']{8,}/i;

export class StoryCheckpoint {
  private base = new Map<string, StoryTask>();
  private delta?: StoryDelta;
  private scope?: Record<string, any>;
  private parent: string | null = null;
  private sequence = 0;
  private unavailable = false;
  private boardMetadata: Record<string, any> = {};

  restore(imported: any, retained: any): { tasks: StoryTask[]; conflicts: Conflict[] } {
    let previous = this.delta;
    let scope = this.scope;
    let parent = this.parent;
    if (retained && !['available', 'absent'].includes(retained.state)) {
      this.unavailable = true;
      throw Error('story_checkpoint_restore_unavailable');
    }
    if (scope && retained?.scope && canonical(scope) !== canonical(retained.scope)) throw Error('story_checkpoint_scope_changed');
    if (!scope && retained?.scope) {
      scope = retained.scope;
      if (retained.state === 'available') {
        const saved = retained.checkpoint;
        if (!saved || !/^[a-f0-9]{64}$/.test(saved.revision) || compatible.some(key => saved.scope?.[key] !== scope![key] && !(key === 'accepted_plan_version' && retained.preserved_plan === true))) throw Error('story_checkpoint_scope_changed');
        previous = saved.delta;
        parent = saved.revision;
      }
    }
    if (scope && (scope.issue !== imported.issue || scope.repository_id !== imported.repository_id || scope.repository !== imported.repository || scope.branch !== imported.branch)) throw Error('story_checkpoint_scope_changed');
    const source = Object.fromEntries(['issue', 'repository_id', 'branch', 'path', 'sha256', 'confirmed_source_revision'].map(key => [key, imported[key]])) as StoryDelta['source'];
    if (previous && (previous.version !== 1 || previous.progress_only !== true || previous.source.issue !== source.issue || previous.source.repository_id !== source.repository_id || previous.source.branch !== source.branch || previous.source.path !== source.path)) throw Error('story_checkpoint_invalid');
    const base = new Map<string, StoryTask>(imported.board.tasks.map((task: StoryTask) => [task.id, task]));
    const boardMetadata = { ...imported.board, tasks: undefined };
    const delta: StoryDelta = { version: 1, progress_only: true, source, updates: {}, additions: {}, conflicts: previous?.conflicts.slice() ?? [], history: previous?.history.slice() ?? [] };
    const conflict = (value: Conflict) => { if (!delta.conflicts.some(item => canonical(item) === canonical(value))) delta.conflicts.push(value); };
    for (const [key, update] of Object.entries(previous?.updates ?? {})) {
      if (!id.test(key) || !/^[a-f0-9]{64}$/.test(update.base_task_hash) || !states.includes(update.status)) throw Error('story_checkpoint_invalid');
      const current = base.get(key);
      if (!current || taskHash(current) !== update.base_task_hash) conflict({ id: key, reason: current ? 'branch_edited' : 'branch_deleted', claimed_status: update.status });
      else delta.updates[key] = update;
    }
    for (const [key, task] of Object.entries(previous?.additions ?? {})) {
      if (!id.test(key) || task.id !== key || !states.includes(task.status) || !['code', 'test', 'infra'].includes(task.kind) || typeof task.title !== 'string' || task.title.length > 1000 || /[\r\n\0]/.test(task.title)) throw Error('story_checkpoint_invalid');
      if (base.has(key)) conflict({ id: key, reason: 'addition_incorporated', claimed_status: task.status });
      else delta.additions[key] = task;
    }
    this.check(delta, base, boardMetadata);
    // Commit only after the entire restore validates; a failed refresh changes no claims.
    this.base = base; this.delta = delta; this.scope = scope; this.parent = parent;
    this.boardMetadata = boardMetadata; this.unavailable = false;
    return { tasks: this.tasks(delta, base), conflicts: delta.conflicts };
  }

  update(tasks: StoryTask[]): void {
    if (this.unavailable) throw Error('story_checkpoint_restore_unavailable');
    if (!this.delta) return;
    const saved = this.delta;
    this.delta = { ...saved, updates: { ...saved.updates }, additions: { ...saved.additions }, history: [...saved.history] };
    try {
      for (const task of tasks) {
        if (!id.test(task.id) || !states.includes(task.status)) throw Error('story_checkpoint_invalid');
        const base = this.base.get(task.id);
        const previous = saved.updates[task.id]?.status ?? saved.additions[task.id]?.status ?? base?.status;
        if (this.scope && previous !== task.status) this.delta.history.push({ id: task.id, status: task.status, run_id: this.scope.run_id, observed_at: new Date().toISOString() });
        if (base) this.delta.updates[task.id] = { base_task_hash: taskHash(base), status: task.status };
        else this.delta.additions[task.id] = { id: task.id, kind: task.kind, title: task.title, status: task.status };
      }
      this.check();
    } catch (error) { this.delta = saved; throw error; }
  }

  private tasks(delta: StoryDelta, base: Map<string, StoryTask>): StoryTask[] {
    return [...base.values()].map(task => ({ ...task, status: delta.updates[task.id]?.status ?? task.status })).concat(Object.values(delta.additions));
  }

  private check(delta = this.delta!, base = this.base, metadata = this.boardMetadata): void {
    for (const [key, task] of Object.entries(delta.additions)) {
      if (!id.test(key) || task.id !== key || !states.includes(task.status) || !['code', 'test', 'infra'].includes(task.kind)
          || typeof task.title !== 'string' || !task.title.trim() || task.title.length > 1000 || /[\r\n\0]/.test(task.title)
          || Object.keys(task).some(key => !['id', 'kind', 'title', 'status'].includes(key))) throw Error('story_checkpoint_invalid');
    }
    const effective = this.tasks(delta, base);
    const text = JSON.stringify(delta);
    if (Buffer.byteLength(text) > CHECKPOINT_BYTES || delta.conflicts.length > 1000 || delta.history.length > 1000
        || effective.length > 1000 || Buffer.byteLength(JSON.stringify({ ...metadata, tasks: effective })) > 48 * 1024) throw Error('story_checkpoint_oversized');
    if (containsSecret(text) || privateSecret.test(text) || Object.entries(process.env).some(([key, value]) => /TOKEN|SECRET|PASSWORD|PRIVATE_KEY|ACCESS_KEY|API_KEY/.test(key) && value && value.length >= 8 && text.includes(value))) throw Error('story_checkpoint_sensitive_content');
  }

  persist(): string | undefined {
    const path = process.env.ADP_STORY_CHECKPOINT_FILE;
    if (!path) return;
    if (!this.scope || !this.delta || this.unavailable) return 'story_checkpoint_restore_unavailable';
    try {
      this.check();
      const envelope = { version: 1, invocation_id: this.scope.run_id, sequence: ++this.sequence, parent_revision: this.parent, story_checkpoint: { scope: this.scope, delta: this.delta } };
      const text = JSON.stringify(envelope);
      if (Buffer.byteLength(text) > 128 * 1024) return 'story_checkpoint_oversized';
      writeFileSync(path + '.tmp', text, { mode: 0o600 });
      renameSync(path + '.tmp', path);
    } catch { return 'story_checkpoint_write_failed'; }
  }
}
