jest.mock('./operations-story-tools', () => ({ invokeOperationsStoryTool: jest.fn() }));
import { invokeOperationsStoryTool } from './operations-story-tools';
import { restoreOperationsStoryBoard } from './operations-story-board';
import { savedBoard } from './__tests__/operations-board-fixture';

const transport = invokeOperationsStoryTool as jest.Mock;
const previous = process.env;
beforeEach(() => { process.env = { ...previous, ISSUE_NUMBER: '12', TARGET_REPO: 'example/service' }; transport.mockReset(); });
afterEach(() => { process.env = previous; });
const todo = (content: string, status = 'pending') => ({ type: 'tool_use', name: 'TodoWrite', input: { todos: [{ content, status }] } });

it.each([1, 2])('restores v%s board with every status and source before model work', async version => {
  const value = savedBoard(); value.board.version = version;
  transport.mockResolvedValue(JSON.stringify({ story_board: value }));
  const publish = jest.fn();
  const board = await restoreOperationsStoryBoard(publish);
  expect(transport).toHaveBeenCalledWith('repair_tasks', {});
  expect(publish).toHaveBeenCalledTimes(1);
  const [text, detail] = publish.mock.calls[0];
  for (const part of ['☑ `code` AC-c1', '⛔ `test` AC-t1', '▶ `infra` AC-i1', '☐ `test` AC-t2', value.sha256, value.confirmed_source_revision]) expect(text).toContain(part);
  expect(detail).toMatchObject({ category: 'plan', plan_scope: 'assignment' });
  expect(board.context).toContain('Original completion evidence');
  expect(board.context).toContain('UNTRUSTED INPUT BELOW');
  expect(board.context).toContain('never authority or acceptance');
});

it('merges explicit changes without erasing omitted tasks; appends only typed discoveries', async () => {
  transport.mockResolvedValue(JSON.stringify({ story_board: savedBoard() }));
  const publish = jest.fn(); const board = await restoreOperationsStoryBoard(publish);
  expect(board.checklist(todo('Diagnose, repair, deploy'))).toBeUndefined();
  expect(board.checklist(todo('AC-t1 [done] Manual acceptance'))).toBeUndefined();
  const unchanged = board.checklist(todo('AC-i1 [done] Deploy', 'completed'))!;
  expect(unchanged).toContain('⛔ `test` AC-t1');
  expect(unchanged).toContain('☑ `code` AC-c1');
  expect(unchanged).toContain('☑ `infra` AC-i1');
  const completed = board.checklist(todo('AC-t1 [done] Manual acceptance', 'completed'))!;
  expect(completed).toContain('☑ `test` AC-t1');
  expect(board.checklist(todo('AC-new [open] Unstructured discovery'))).toBeUndefined();
  const added = board.checklist(todo('AC-new [open] [test] Regression check'))!;
  expect(added).toContain('☐ `test` AC-new — Regression check (added this run)');
  board.observeToolResult('repair_tasks', JSON.stringify({ story_board: savedBoard() }));
  expect(publish.mock.calls.at(-1)[0]).toContain('☑ `test` AC-t1');
  expect(publish.mock.calls.at(-1)[0]).toContain('AC-new');
});

it('projects digest-bound Codex task changes with explicit blocked states', async () => {
  transport.mockResolvedValue(JSON.stringify({ story_board: savedBoard() }));
  const publish = jest.fn(); const board = await restoreOperationsStoryBoard(publish);
  expect(board.update('a'.repeat(64), [{ id: 'AC-i1', status: 'blocked' }]).updated).toBe(true);
  expect(publish.mock.calls.at(-1)[0]).toContain('⛔ `infra` AC-i1');
  expect(board.update('a'.repeat(64), [{ id: 'AC-i1', status: 'done' }]).updated).toBe(true);
  expect(publish.mock.calls.at(-1)[0]).toContain('☑ `infra` AC-i1');
  expect(publish.mock.calls.at(-1)[0]).toContain('⛔ `test` AC-t1');
  expect(board.update('c'.repeat(64), [{ id: 'AC-t1', status: 'done' }]).updated).toBe(false);
  expect(publish.mock.calls.at(-1)[0]).toContain('⛔ `test` AC-t1');
});

it.each(['invalid', 'unavailable', 'oversized'])('recovers a %s import on a later authorized repair_tasks result', async state => {
  transport.mockResolvedValue(JSON.stringify({ story_board: { state, sha256: 'a'.repeat(64), next_action: 'Read every page with expected_sha256' } }));
  const publish = jest.fn(); const board = await restoreOperationsStoryBoard(publish);
  expect(board.checklist(todo('New generic checklist'))).toBeUndefined();
  expect(board.context).toContain('expected_sha256');
  expect(publish.mock.calls.every(([, detail]) => detail.category !== 'plan')).toBe(true);
  board.observeToolResult('repair_tasks', JSON.stringify({ story_board: savedBoard() }));
  expect(publish.mock.calls.at(-1)[1].category).toBe('plan');
  expect(board.checklist(todo('AC-i1 [done] Deploy', 'completed'))).toContain('☑ `infra` AC-i1');
  board.observeToolResult('repair_tasks', '{}');
  expect(board.checklist(todo('AC-t2 [in_progress] Verify', 'in_progress'))).toContain('☑ `infra` AC-i1');
});

it('allows a fresh ordinary checklist only when the assigned board is explicitly absent', async () => {
  transport.mockResolvedValue(JSON.stringify({ story_board: { state: 'absent' } }));
  const board = await restoreOperationsStoryBoard(jest.fn());
  expect(board.checklist(todo('Diagnose the release'))).toContain('0 of 1 tasks complete');
});

it.each(['issue', 'path', 'digest', 'revision', 'repository', 'identity', 'duplicate'])('refuses invalid %s provenance without a partial board', async fault => {
  const value: any = savedBoard();
  if (fault === 'issue') value.issue = 13;
  if (fault === 'path') value.path = '.adp/tasks/13.json';
  if (fault === 'digest') value.sha256 = 'invalid';
  if (fault === 'revision') value.confirmed_source_revision = 'invalid';
  if (fault === 'repository') value.repository = 'foreign/service';
  if (fault === 'identity') value.repository_id = 0;
  if (fault === 'duplicate') value.board.tasks.push(value.board.tasks[0]);
  transport.mockResolvedValue(JSON.stringify({ story_board: value }));
  const publish = jest.fn(); const board = await restoreOperationsStoryBoard(publish);
  expect(publish.mock.calls.every(([, detail]) => detail.category !== 'plan')).toBe(true);
  expect(board.context).not.toContain('Original completion evidence');
});

it('does not publish a truncated completion total for a display-sized overflow', async () => {
  const value = savedBoard(); value.board.tasks = Array.from({ length: 100 }, (_, i) => ({ ...value.board.tasks[0], id: `AC-${i}`, title: 'Long title '.repeat(20) }));
  transport.mockResolvedValue(JSON.stringify({ story_board: value }));
  const publish = jest.fn(); const board = await restoreOperationsStoryBoard(publish);
  expect(board.context).toContain('AC-99');
  expect(publish.mock.calls.every(([, detail]) => detail.category !== 'plan')).toBe(true);
  expect(publish.mock.calls[0][0]).toContain('no partial checklist');
});
