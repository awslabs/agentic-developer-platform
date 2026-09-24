/**
 * Tests for the update_draft tool (#4208).
 *
 * The load-bearing test here is cross-session isolation: the tool takes its
 * session from the closure, so an invocation whose arguments try to name another
 * session must affect zero rows in that other session.
 */
import { draftToolsForTurn } from './tools';
import { DraftStore, IntentDraft } from './port';

class FakeDraftStore implements DraftStore {
  public writes: Array<{ sessionId: string; draft: IntentDraft }> = [];
  private drafts = new Map<string, IntentDraft>();

  async get(sessionId: string): Promise<IntentDraft | null> {
    return this.drafts.get(sessionId) ?? null;
  }

  async put(sessionId: string, draft: IntentDraft): Promise<IntentDraft> {
    const stored: IntentDraft = { ...draft, updatedAt: '2026-08-28T00:00:00.000Z' };
    this.writes.push({ sessionId, draft: stored });
    this.drafts.set(sessionId, stored);
    return stored;
  }
}

function getTool(store: DraftStore, sessionId = 'sess-a', onUpdate?: (d: IntentDraft) => void) {
  const tools = draftToolsForTurn(store, { sessionId, onUpdate });
  const tool = tools.find(t => t.name === 'update_draft');
  if (!tool) throw new Error('update_draft not registered');
  return tool;
}

describe('update_draft — session scoping', () => {
  it('writes to the closure session', async () => {
    const store = new FakeDraftStore();
    await getTool(store, 'sess-a').handler({ intent: 'Nightly cost report' });

    expect(store.writes).toHaveLength(1);
    expect(store.writes[0].sessionId).toBe('sess-a');
    expect(store.writes[0].draft.intent).toBe('Nightly cost report');
  });

  it('ignores a session id supplied in tool arguments — victim session untouched', async () => {
    const store = new FakeDraftStore();
    // Seed a draft for the victim so we can prove it is not overwritten.
    await store.put('sess-victim', { intent: 'victim original intent' });
    const writesBefore = store.writes.length;

    await getTool(store, 'sess-a').handler({
      intent: 'attacker intent',
      // None of these are in the schema; all must be ignored.
      session_id: 'sess-victim',
      sessionId: 'sess-victim',
      PK: 'session#sess-victim',
    });

    // The victim's draft is byte-for-byte unchanged.
    expect((await store.get('sess-victim'))!.intent).toBe('victim original intent');
    // Exactly one new write, and it landed on the closure's session.
    expect(store.writes.slice(writesBefore)).toHaveLength(1);
    expect(store.writes[store.writes.length - 1].sessionId).toBe('sess-a');
  });

  it('does not expose a session argument in its input schema', () => {
    const tool = getTool(new FakeDraftStore());
    const keys = Object.keys(tool.inputSchema);
    expect(keys).not.toContain('session_id');
    expect(keys).not.toContain('sessionId');
    expect(keys.sort()).toEqual(['constraints', 'epic_display', 'intent', 'motivation', 'open_questions', 'outcomes', 'wave_display']);
  });
});

describe('update_draft — field handling', () => {
  it('maps open_questions to openQuestions and keeps lists', async () => {
    const store = new FakeDraftStore();
    await getTool(store).handler({
      intent: 'Ship a cost report',
      motivation: 'Finance asks for it monthly',
      outcomes: ['Report lands in Slack by 9am'],
      constraints: ['Must use existing Cost Explorer data'],
      open_questions: ['Which account scope?'],
    });

    const { draft } = store.writes[0];
    expect(draft.openQuestions).toEqual(['Which account scope?']);
    expect(draft.outcomes).toEqual(['Report lands in Slack by 9am']);
    expect(draft.constraints).toEqual(['Must use existing Cost Explorer data']);
    expect(draft.motivation).toBe('Finance asks for it monthly');
  });

  it('is a whole-object replace — omitted fields are cleared, not merged', async () => {
    const store = new FakeDraftStore();
    const tool = getTool(store);
    await tool.handler({ intent: 'first', motivation: 'because' });
    await tool.handler({ intent: 'second' });

    const latest = store.writes[1].draft;
    expect(latest.intent).toBe('second');
    expect(latest.motivation).toBeUndefined();
  });

  it('drops empty and non-string values instead of storing junk', async () => {
    const store = new FakeDraftStore();
    await getTool(store).handler({
      intent: '   ',
      motivation: 42,
      outcomes: ['ok', '', null, 7],
      constraints: 'not-a-list',
    });

    const { draft } = store.writes[0];
    expect(draft.intent).toBeUndefined();
    expect(draft.motivation).toBeUndefined();
    expect(draft.outcomes).toEqual(['ok']);
    expect(draft.constraints).toBeUndefined();
  });

  it('caps list length and field size so a runaway model cannot write an unbounded row', async () => {
    const store = new FakeDraftStore();
    await getTool(store).handler({
      intent: 'x'.repeat(5000),
      outcomes: Array.from({ length: 100 }, (_, i) => `outcome ${i}`),
    });

    const { draft } = store.writes[0];
    expect(draft.intent!.length).toBe(2000);
    expect(draft.outcomes!.length).toBe(20);
  });

  it('reports a cleared draft rather than silently writing nothing', async () => {
    const store = new FakeDraftStore();
    const result = await getTool(store).handler({});

    expect(store.writes).toHaveLength(1);
    expect(result.content[0].text).toContain('cleared');
  });
});

describe('update_draft — panel notification', () => {
  it('invokes onUpdate with the stored draft so STATE_DELTA can be emitted', async () => {
    const store = new FakeDraftStore();
    const seen: IntentDraft[] = [];
    await getTool(store, 'sess-a', d => seen.push(d)).handler({ intent: 'Nightly cost report' });

    expect(seen).toHaveLength(1);
    expect(seen[0].intent).toBe('Nightly cost report');
    // The callback receives the STORED draft, including the store's timestamp,
    // so the panel shows the same thing that was persisted.
    expect(seen[0].updatedAt).toBe('2026-08-28T00:00:00.000Z');
  });

  it('surfaces which fields are populated back to the model', async () => {
    const store = new FakeDraftStore();
    const result = await getTool(store).handler({ intent: 'a', outcomes: ['b'] });

    expect(result.content[0].text).toContain('intent');
    expect(result.content[0].text).toContain('outcomes');
    expect(result.isError).toBeUndefined();
  });
});


it('persists model-chosen epic and wave descriptions in the session draft', async () => {
  const store = new FakeDraftStore();
  const epic = { title: 'External tasks', description: 'Services need a task lifecycle without GitHub. Preserve tenant isolation and durable acceptance.' };
  const wave = { title: 'Task submission and dispatch', description: 'Implement durable submission and recoverable dispatch; qualify authorization and storage.' };
  await getTool(store).handler({ intent: 'External invocation', epic_display: epic, wave_display: wave });
  expect(store.writes[0].draft.epicDisplay).toEqual(epic);
  expect(store.writes[0].draft.waveDisplay).toEqual(wave);
  expect(store.writes[0].draft.openQuestions).toBeUndefined();
});

it.each(['epic_display', 'wave_display'])('rejects malformed %s without overwriting the draft', async field => {
  const store = new FakeDraftStore();
  const result = await getTool(store).handler({ [field]: { title: ' ', description: 'x'.repeat(3001) } });
  expect(result.isError).toBe(true);
  expect(store.writes).toHaveLength(0);
});
