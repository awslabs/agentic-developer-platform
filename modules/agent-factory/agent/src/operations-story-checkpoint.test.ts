jest.mock('./operations-story-tools', () => ({ invokeOperationsStoryTool: jest.fn() }));
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { invokeOperationsStoryTool } from './operations-story-tools';
import { restoreOperationsStoryBoard } from './operations-story-board';
import { canonical, StoryCheckpoint, taskHash } from './operations-story-checkpoint';
import { savedBoard } from './__tests__/operations-board-fixture';

const transport = invokeOperationsStoryTool as jest.Mock;
const old = process.env;
let directory: string;
const scope = (attempt = 1) => ({ tenant_id: 'org', flow_id: 'flow', node_id: 'node', issue: 12,
  repository_id: 7, installation_id: 1, repository: 'example/service', branch: 'agent/issue-12',
  accepted_plan_version: 1, run_id: `run-${attempt}`, attempt, claim_generation: attempt });
const path = () => process.env.ADP_STORY_CHECKPOINT_FILE!;
const privateFile = () => JSON.parse(readFileSync(path(), 'utf8'));
const initial = () => ({ state: 'absent', scope: scope(), checkpoint: null });
const restored = (file: any, attempt = 2) => ({ state: 'available', scope: scope(attempt),
  preserved_plan: true, checkpoint: { ...file.story_checkpoint, revision: 'c'.repeat(64) } });
beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), 'adp-board-test-'));
  process.env = { ...old, ISSUE_NUMBER: '12', TARGET_REPO: 'example/service', ADP_STORY_CHECKPOINT_FILE: join(directory, 'private.json') };
  transport.mockReset();
});
afterEach(() => { process.env = old; rmSync(directory, { recursive: true, force: true }); });

it.each(['claude', 'codex'])('%s saves the shared merge and restores cumulative task claims on restart', async stack => {
  transport.mockResolvedValue(JSON.stringify({ story_board: savedBoard(), story_checkpoint: initial() }));
  const publish = jest.fn();
  const first = await restoreOperationsStoryBoard(publish);
  if (stack === 'claude') first.checklist({ type: 'tool_use', name: 'TodoWrite', input: { todos: [
    { content: 'AC-t1 [done] Completed check', status: 'completed' },
    { content: 'NEW-1 [blocked] [infra] Select genuine inputs', status: 'pending' },
  ] } });
  else first.update('a'.repeat(64), [{ id: 'AC-t1', status: 'done' }, { id: 'NEW-1', kind: 'infra', title: 'Select genuine inputs', status: 'blocked' }]);
  const saved = privateFile();
  expect(saved.story_checkpoint.delta.updates['AC-t1'].status).toBe('done');
  expect(saved.story_checkpoint.delta.additions['NEW-1'].status).toBe('blocked');
  expect(saved.story_checkpoint.delta.history).toHaveLength(2);
  expect(JSON.stringify(publish.mock.calls)).not.toContain('base_task_hash');
  transport.mockResolvedValue(JSON.stringify({ story_board: savedBoard(), story_checkpoint: restored(saved) }));
  const second = await restoreOperationsStoryBoard(jest.fn());
  expect(second.context).toContain('NEW-1');
  expect(second.context).toContain('effective progress claims');
  second.update('a'.repeat(64), [{ id: 'AC-t2', status: 'in_progress' }]);
  const next = privateFile();
  expect(next.parent_revision).toBe('c'.repeat(64));
  expect(next.story_checkpoint.delta.updates['AC-t1'].status).toBe('done');
  expect(next.story_checkpoint.delta.additions['NEW-1']).toBeDefined();
  expect(next.story_checkpoint.delta.history.at(-1).run_id).toBe('run-2');
});

it('branch edits and deletions win; incorporated additions do not resurrect after deletion', () => {
  const state = new StoryCheckpoint(); const imported: any = savedBoard();
  state.restore(imported, initial());
  state.update([{ ...imported.board.tasks[1], status: 'done' }, { ...imported.board.tasks[2], status: 'done' },
    { id: 'NEW-1', kind: 'code', title: 'Discovered work', status: 'done' }]);
  const changed: any = savedBoard(); changed.board.tasks[1].note = 'Owner correction';
  changed.board.tasks = changed.board.tasks.filter((t: any) => t.id !== 'AC-i1');
  changed.board.tasks.push({ id: 'NEW-1', kind: 'code', title: 'Owner task', status: 'open' } as any);
  const result = state.restore(changed, initial());
  expect(result.tasks.find(t => t.id === 'AC-t1')?.status).toBe('blocked');
  expect(result.tasks.some(t => t.id === 'AC-i1')).toBe(false);
  expect(result.tasks.find(t => t.id === 'NEW-1')?.title).toBe('Owner task');
  expect(result.conflicts.map(t => t.reason)).toEqual(['branch_edited', 'branch_deleted', 'addition_incorporated']);
  changed.board.tasks = changed.board.tasks.filter((t: any) => t.id !== 'NEW-1');
  expect(state.restore(changed, initial()).tasks.some(t => t.id === 'NEW-1')).toBe(false);
});

it('explicit later correction may reopen done; repeated import preserves it', () => {
  const state = new StoryCheckpoint(); const imported: any = savedBoard(); state.restore(imported, initial());
  state.update([{ ...imported.board.tasks[0], status: 'open' }]);
  expect(state.restore(imported, initial()).tasks[0].status).toBe('open');
  expect(taskHash({ ...imported.board.tasks[0] })).toBe(taskHash(imported.board.tasks[0]));
  expect(canonical({ z: 1, a: 2 })).toBe('{"a":2,"z":1}');
});

it.each(['tenant_id', 'node_id', 'flow_id', 'repository_id', 'branch', 'accepted_plan_version'])('rejects foreign %s checkpoint', field => {
  const state = new StoryCheckpoint(); state.restore(savedBoard(), initial()); state.persist();
  const retained: any = restored(privateFile()); retained.preserved_plan = false;
  retained.checkpoint.scope[field] = field.endsWith('id') && field === 'repository_id' ? 99 : 'foreign';
  expect(() => new StoryCheckpoint().restore(savedBoard(), retained)).toThrow('scope_changed');
});

it('saves privately even when public rendering overflows, without leaking checkpoint data', async () => {
  const imported: any = savedBoard(); imported.board.tasks = Array.from({ length: 100 }, (_, i) => ({ ...imported.board.tasks[0], id: `AC-${i}`, title: 'Long task '.repeat(20) }));
  transport.mockResolvedValue(JSON.stringify({ story_board: imported, story_checkpoint: initial() }));
  const publish = jest.fn(); const board = await restoreOperationsStoryBoard(publish);
  expect(board.update('a'.repeat(64), [{ id: 'AC-99', status: 'blocked' }]).updated).toBe(true);
  expect(privateFile().story_checkpoint.delta.updates['AC-99'].status).toBe('blocked');
  expect(publish.mock.calls.every(([, detail]) => detail.category !== 'plan')).toBe(true);
  expect(JSON.stringify(publish.mock.calls)).not.toContain('base_task_hash');
});

it('refuses sensitive or oversized changes without overwriting the prior private checkpoint', () => {
  const state = new StoryCheckpoint(); state.restore(savedBoard(), initial()); state.persist();
  const before = readFileSync(path(), 'utf8');
  expect(() => state.update([{ id: 'NEW', kind: 'code', status: 'open', title: 'Bearer do-not-persist-this-value' }])).toThrow('sensitive');
  expect(() => state.update(Array.from({ length: 100 }, (_, i) => ({ id: `NEW-${i}`, kind: 'code' as const, status: 'open' as const, title: 'x'.repeat(1000) })))).toThrow('oversized');
  expect(readFileSync(path(), 'utf8')).toBe(before);
});

it('rejects oversized final addition titles and invalid additions before persistence', async () => {
  transport.mockResolvedValue(JSON.stringify({ story_board: savedBoard(), story_checkpoint: initial() }));
  const board = await restoreOperationsStoryBoard(jest.fn());
  expect(board.update('a'.repeat(64), [{ id: 'NEW', kind: 'code', status: 'open', title: 'x'.repeat(1000) }]).updated).toBe(false);
  expect(board.update('a'.repeat(64), [{ id: 'NEW', kind: 'code', status: 'open', title: 'x'.repeat(983) }]).updated).toBe(true);
  expect(privateFile().story_checkpoint.delta.additions.NEW.title).toHaveLength(1000);
  const state = new StoryCheckpoint(); state.restore(savedBoard(), initial());
  expect(() => state.update([{ id: 'NEW', kind: 'code', status: 'open', title: '' }])).toThrow('invalid');
});

it('failed restore preserves the old state and unavailable reads recover without losing prior claims', () => {
  const imported: any = savedBoard(); const state = new StoryCheckpoint();
  state.restore(imported, initial()); state.update([{ ...imported.board.tasks[1], status: 'done' }]); state.persist();
  const prior = privateFile();
  const tooLarge: any = savedBoard(); tooLarge.board.tasks[0].note = 'x'.repeat(48 * 1024);
  expect(() => state.restore(tooLarge, initial())).toThrow('oversized');
  expect(state.restore(imported, initial()).tasks[1].status).toBe('done');
  expect(() => state.restore(imported, { state: 'unavailable', scope: scope() })).toThrow('unavailable');
  expect(() => state.update([{ ...imported.board.tasks[2], status: 'done' }])).toThrow('unavailable');
  expect(state.restore(imported, { ...restored(prior, 1), scope: scope() }).tasks[1].status).toBe('done');
  state.update([{ ...imported.board.tasks[2], status: 'done' }]); state.persist();
  expect(privateFile().story_checkpoint.delta.updates['AC-t1'].status).toBe('done');
  const fresh = new StoryCheckpoint();
  expect(() => fresh.restore(imported, { state: 'unavailable', scope: scope() })).toThrow('unavailable');
  expect(fresh.restore(imported, restored(prior)).tasks[1].status).toBe('done');
});

it('refuses a merged effective board beyond the model context bound without partially importing it', () => {
  const imported: any = savedBoard(); imported.board.tasks[0].note = 'x'.repeat(47000);
  const state = new StoryCheckpoint(); state.restore(imported, initial());
  expect(() => state.update(Array.from({ length: 10 }, (_, i) => ({ id: `NEW-${i}`, kind: 'code' as const, title: 'x'.repeat(500), status: 'open' as const })))).toThrow('oversized');
  expect(state.restore(imported, initial()).tasks).toHaveLength(4);
});
