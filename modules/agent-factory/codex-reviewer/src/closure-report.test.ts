import test from 'node:test';
import assert from 'node:assert/strict';
import { parseReviewClosure, reconcileReviewedTasks, reviewClosureReport } from './closure-report.js';
import type { Task } from './task-board.js';

const tasks: Task[] = [
  { id: 'ui', title: 'Workspace UI', kind: 'code', criterion: 'AC1', status: 'done', covers: [], files: [], note: 'Implemented' },
  { id: 'browser', title: 'Browser checks', kind: 'test', criterion: 'AC1', status: 'open', covers: ['ui'], files: [], note: 'Await CI' },
  { id: 'live', title: 'Live workspace demo', kind: 'test', criterion: 'AC1', status: 'blocked', covers: ['ui'], files: [], note: 'Owned by evaluator #5540' },
];
test('only explicit verified task IDs close; unknown and deferred tasks never become done', () => {
  const closure = parseReviewClosure({ completed: ['The workspace screen passed browser checks.'], remaining: [],
    verifiedTasks: [{ id: 'browser', evidence: 'Browser CI passed for the reviewed commit' },
      { id: 'live', evidence: 'Should stay deferred' }, { id: 'unknown', evidence: 'Not a board task' }] });
  const board = reconcileReviewedTasks(tasks, closure, 'a'.repeat(40));
  assert.deepEqual(board.map(t => t.status), ['done', 'done', 'blocked']);
  assert.equal(tasks[1]!.status, 'open');
  const report = reviewClosureReport({ summary: 'Workspace UI reviewed.', tasks: board, closure, merged: true, sha: 'a'.repeat(40) });
  assert.equal(report.delivery, 'Pull request merged.');
  assert.match(report.remaining.join(' '), /Live workspace demo.*#5540/);
});
test('missing or malformed reporting cannot invent checklist completion', () => {
  for (const raw of [undefined, {}, { completed: [], remaining: [], verifiedTasks: [{ id: 'browser', evidence: '' }] }]) {
    const closure = parseReviewClosure(raw);
    assert.equal(closure, undefined);
    assert.deepEqual(reconcileReviewedTasks(tasks, closure, 'a'.repeat(40)), tasks);
  }
  const report = reviewClosureReport({ summary: 'CI remains pending.', tasks, merged: false, sha: 'a'.repeat(40), blocker: 'CI deadline reached' });
  assert.match(report.delivery, /not been verified merged/);
  assert.ok(report.remaining.includes('CI deadline reached'));
});
