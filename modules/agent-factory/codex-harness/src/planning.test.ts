import test from 'node:test';
import assert from 'node:assert/strict';
import { parsePlanning, planningContract, planningReport, readyAssignments, storyMarker, planningIssueContext, planningCorrection } from './planning.js';
import { PlanningProvider, type PlanningTransport } from './planning-provider.js';
const base = { summary: 'Plan', requirements: [{ id: 'one', text: 'Retain audit history', source_refs: ['instructions'] }], assumptions: [], open_questions: [], superseded_requirements: [] };
const product = { ...base, acceptance_criteria: ['History remains after editing'] };
const refs = new Set(['instructions', 'follow_up_input.real']);
const parse = (artifact: unknown, previous?: typeof product) => parsePlanning(JSON.stringify({ artifact, clarification: null }), 'product', refs, previous);
test('requirements cannot disappear or change without a real amendment', () => {
  assert.throws(() => parse({ ...product, requirements: [{ ...base.requirements[0], text: 'Remove history' }] }, product), /silently/);
  assert.throws(() => parse({ ...product, superseded_requirements: [{ id: 'one', reason: 'requested', source_ref: 'invented' }] }, product), /amendment/);
  assert.doesNotThrow(() => parse({ ...product, requirements: [{ ...base.requirements[0], text: 'Keep history for 90 days', source_refs: ['follow_up_input.real'] }], superseded_requirements: [{ id: 'one', reason: 'retention clarified', source_ref: 'follow_up_input.real' }] }, product));
  assert.throws(() => parse({ ...product, requirements: [{ ...base.requirements[0], source_refs: ['invented'] }] }), /source/);
});
test('partial intake drafts retain uncertainty and cannot set authority', () => {
  const value = { artifact: { ...base, draft: { intent: 'Audit edits', openQuestions: ['How long?'] } }, clarification: 'How long must history be retained?' };
  assert.equal(parsePlanning(JSON.stringify(value), 'intent-refinement', refs).artifact.summary, 'Plan');
  assert.throws(() => parsePlanning(JSON.stringify({ ...value, artifact: { ...value.artifact, draft: { ...value.artifact.draft, persona: 'developer' } } }), 'intent-refinement', refs));
});
test('architect rejects dependency cycles and unknown blockers', () => {
  const story = { key: 'a', title: 'Audit', description: 'Store history', acceptance_criteria: ['Query history'], source_refs: ['instructions'], blocked_by: ['b'] };
  const value = { artifact: { ...base, design: 'Append log', publish_stories: true, stories: [story, { ...story, key: 'b', blocked_by: ['a'] }] }, clarification: null };
  assert.throws(() => parsePlanning(JSON.stringify(value), 'architect', refs), /Cyclic/);
  value.artifact.stories.pop();
  assert.throws(() => parsePlanning(JSON.stringify(value), 'architect', refs), /dependency/);
});
test('missing and cancelled dependencies block scheduling', () => {
  assert.deepEqual(readyAssignments([{ issue: 1, state: 'open', blockedBy: [2] }], [{ issue: 1, persona: 'codex-developer' }]), []);
  assert.deepEqual(readyAssignments([{ issue: 1, state: 'open', blockedBy: [2] }, { issue: 2, state: 'cancelled', blockedBy: [] }], [{ issue: 1, persona: 'codex-developer' }]), []);
});
test('planning report preserves the structured artifact and exact provenance', () => {
  const report = planningReport('product', product, new Map([['instructions', { ref: 'instructions', source: 'instructions' }]]));
  assert.deepEqual(JSON.parse(report.documents[0]!.content), product);
  assert.deepEqual(report.evidence_refs, [{ ref: 'instructions', source: 'instructions' }]);
});
for (const provider of ['github', 'gitlab'] as const) {
  test(`${provider}: dispatch rechecks state and confirmed retries do not dispatch twice`, async () => {
    const receipts = new Map<string, unknown>(); let dispatched = 0; let blocked = false;
    const issue = (n: number, completed = false) => provider === 'github'
      ? { id: n + 100, number: n, state: completed ? 'closed' : 'open', state_reason: completed ? 'completed' : null, title: 'Story', body: 'ADP parent: 10\n' }
      : { id: n + 100, iid: n, state: completed ? 'closed' : 'opened', title: 'Story', description: 'ADP parent: 10\n', link_type: 'is_blocked_by' };
    const host: PlanningTransport = {
      async read(path) {
        if (path.includes('/timeline?') || path.includes('/related_merge_requests?')) return [];
        if (path.includes('/10/')) return [issue(1)];
        if (path.includes('/1/')) return [issue(2, !blocked)];
        if (path.endsWith('/2')) return issue(2, !blocked);
        throw new Error(path);
      },
      async write() { throw new Error('unexpected mutation'); },
      async effect(key, _request, execute) { if (!receipts.has(key)) receipts.set(key, await execute()); return receipts.get(key); },
      async dispatch() { dispatched++; return { invocation: 'child' }; },
    };
    const planner = new PlanningProvider(provider, 'owner/repo', 10, host);
    const assignments = [{ issue: 1, persona: 'codex-developer' }];
    assert.equal((await planner.schedule(assignments))[0]!.status, 'dispatched');
    await planner.schedule(assignments);
    assert.equal(dispatched, 1);
    blocked = true;
    assert.equal((await planner.schedule(assignments))[0]!.status, 'blocked_or_ineligible');
    assert.equal(dispatched, 1);
    await assert.rejects(planner.schedule([...assignments, ...assignments]), /Duplicate/);
  });
}
test('story publication retries reuse create and relationship receipts', async () => {
  const receipts = new Map(); let writes = 0;
  const planner = new PlanningProvider('github', 'owner/repo', 10, {
    async read() { return []; }, async dispatch() { throw new Error('unexpected dispatch'); },
    async effect(key, request, execute) {
      const prior = receipts.get(key);
      if (prior) { assert.deepEqual(prior.request, request); return prior.result; }
      const result = await execute(); receipts.set(key, {request, result}); return result;
    },
    async write(path, body) { writes++; return { id: 101, number: 1, state: 'open', title: body.title ?? 'link', body: body.body ?? '' }; },
  });
  const artifact = { ...base, design: 'Append log', publish_stories: true, stories: [{ key: 'audit', title: 'Audit', description: 'Store history', acceptance_criteria: ['Query history'], blocked_by: [], source_refs: ['instructions'] }] };
  assert.deepEqual(await planner.publishStories(artifact), [{key: 'audit', issue: 1}]);
  await planner.publishStories(artifact);
  assert.equal(writes, 2);
  assert.equal(storyMarker(10, 'audit'), storyMarker(10, 'audit'));
});

test('existing open changes prevent a second implementation assignment', () => {
  assert.deepEqual(readyAssignments([{issue: 1, state: 'open', blockedBy: [], assigned: true}], [{issue: 1, persona: 'codex-developer'}]), []);
});


test('planning issue context preserves human text and source IDs without duplicate artifacts or transport metadata', () => {
  const artifact = '```json\n{"artifact":{}}\n```';
  const issue = {number: 1, title: 'Plan', body: 'Preserve all requirements.', url: 'https://example.test/1', state: 'OPEN',
    comments: [{id: 'human', author: {login: 'human'}, body: 'Keep 90 days. <!-- unrelated requirement -->'},
      {id: 'artifact', author: {login: 'bot'}, body: '<!-- adp-run:123 -->\nSummary\n'+artifact+'\nKeep this note.'}]};
  const compact = planningIssueContext(issue, 'artifact', artifact);
  assert.deepEqual(compact.comments[0], {id: 'human', author: 'human', body: issue.comments[0]!.body});
  assert.match(compact.comments[1]!.body, /Summary/);
  assert.match(compact.comments[1]!.body, /previous_artifact/);
  assert.match(compact.comments[1]!.body, /Keep this note/);
  assert.doesNotMatch(compact.comments[1]!.body, /adp-run|```json/);
  assert.equal(issue.comments[1]!.body.includes(artifact), true);
});


test('planning schema repair names the invalid optional field without inventing its value', () => {
  let failure: unknown;
  try { parsePlanning(JSON.stringify({artifact: {...base, draft: {intent: 'Export own data', motivation: ''}}, clarification: 'Which format?'}), 'intent-refinement', refs); }
  catch (error) { failure = error; }
  assert.match(planningCorrection(failure), /artifact.draft.motivation/);
  assert.match(planningCorrection(failure), /Omit unknown optional string fields/);
});

test('direct reports accept long designs and documents while retaining citation and dependency validation', () => {
  const artifact = { ...base, design: 'Detailed architecture. '.repeat(1800).trim(), stories: [], publish_stories: false };
  const raw = JSON.stringify({ artifact, clarification: null });
  assert.ok(Buffer.byteLength(raw) > 32768);
  assert.deepEqual(parsePlanning(raw, 'architect', refs, undefined, true).artifact, artifact);
  assert.throws(() => parsePlanning(raw, 'architect', refs));
  assert.throws(() => parsePlanning(raw, 'architect', new Set(), undefined, true), /source/);
  const story = { key: 'a', title: 'Audit', description: 'Store history', acceptance_criteria: ['Query history'], source_refs: ['instructions'], blocked_by: ['b'] };
  const cyclic = JSON.stringify({ artifact: { ...artifact, stories: [story, { ...story, key: 'b', blocked_by: ['a'] }] }, clarification: null });
  assert.throws(() => parsePlanning(cyclic, 'architect', refs, undefined, true), /Cyclic/);
});

test('direct contracts omit narrative ceilings for every planning persona', () => {
  for (const persona of ['architect', 'product', 'pm', 'intent-refinement'] as const) {
    const direct = planningContract(persona, true);
    assert.doesNotMatch(direct.instruction, /24000/);
    const schema = direct.schema as any;
    assert.equal(schema.properties.artifact.properties.summary.maxLength, undefined);
    assert.equal(schema.properties.artifact.properties.requirements.maxItems, undefined);
    assert.match(planningContract(persona).instruction, /24000/);
  }
});
