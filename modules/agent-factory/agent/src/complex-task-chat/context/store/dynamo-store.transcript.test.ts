/**
 * Tests for the ordered, uncapped transcript retrieval added in #4208.
 *
 * These cover the two things the pre-existing `readContextItems` gets wrong for
 * a hand-off: it drops `LastEvaluatedKey` (so it truncates at the 1 MB page
 * boundary) and it hydrates nothing.
 */
import { DynamoContextStore } from './dynamo-store';

type SentCommand = { name: string; input: Record<string, unknown> };

/**
 * Minimal fake DocumentClient. Routes by command constructor name so we can
 * assert on pagination without standing up DynamoDB Local.
 */
function makeFakeDdb(handlers: {
  query?: (input: Record<string, unknown>) => Record<string, unknown>;
  batchGet?: (input: Record<string, unknown>) => Record<string, unknown>;
  get?: (input: Record<string, unknown>) => Record<string, unknown>;
}) {
  const sent: SentCommand[] = [];
  const ddb = {
    async send(cmd: { constructor: { name: string }; input: Record<string, unknown> }) {
      const name = cmd.constructor.name;
      sent.push({ name, input: cmd.input });
      if (name === 'QueryCommand') return handlers.query?.(cmd.input) ?? { Items: [] };
      if (name === 'BatchGetCommand') return handlers.batchGet?.(cmd.input) ?? { Responses: {} };
      if (name === 'GetCommand') return handlers.get?.(cmd.input) ?? {};
      throw new Error(`unexpected command ${name}`);
    },
  };
  return { ddb, sent };
}

const TABLE = 'adp-test-chat-context';

function itemRow(ordinal: number, ref: string, type: 'msg' | 'sum' = 'msg') {
  return { PK: 'session#s1', SK: `item#${String(ordinal).padStart(8, '0')}`, ordinal, type, ref, tokens: 10 };
}

function msgRow(id: string, role: 'user' | 'assistant', content: string) {
  return { PK: 'session#s1', SK: `msg#${id}`, role, content, ts: '2026-08-28T00:00:00Z', tokens: 10 };
}

describe('readAllContextItems', () => {
  it('drains every page instead of stopping at the first', async () => {
    // 3 pages: 2 items, 2 items, 1 item.
    const pages = [
      { Items: [itemRow(0, 'm0'), itemRow(1, 'm1')], LastEvaluatedKey: { PK: 'session#s1', SK: 'item#00000001' } },
      { Items: [itemRow(2, 'm2'), itemRow(3, 'm3')], LastEvaluatedKey: { PK: 'session#s1', SK: 'item#00000003' } },
      { Items: [itemRow(4, 'm4')] },
    ];
    let call = 0;
    const { ddb, sent } = makeFakeDdb({ query: () => pages[call++] });
    const store = new DynamoContextStore(TABLE, 'us-east-1', ddb as never);

    const items = await store.readAllContextItems('s1');

    expect(items.map(i => i.ordinal)).toEqual([0, 1, 2, 3, 4]);
    expect(sent.filter(s => s.name === 'QueryCommand')).toHaveLength(3);
    // Page 2 and 3 must carry the prior page's LastEvaluatedKey.
    const queries = sent.filter(s => s.name === 'QueryCommand');
    expect(queries[0].input.ExclusiveStartKey).toBeUndefined();
    expect(queries[1].input.ExclusiveStartKey).toEqual({ PK: 'session#s1', SK: 'item#00000001' });
    expect(queries[2].input.ExclusiveStartKey).toEqual({ PK: 'session#s1', SK: 'item#00000003' });
  });

  it('returns ordinal order even if DDB hands back an unsorted page', async () => {
    const { ddb } = makeFakeDdb({
      query: () => ({ Items: [itemRow(3, 'm3'), itemRow(1, 'm1'), itemRow(2, 'm2')] }),
    });
    const store = new DynamoContextStore(TABLE, 'us-east-1', ddb as never);

    const items = await store.readAllContextItems('s1');
    expect(items.map(i => i.ordinal)).toEqual([1, 2, 3]);
  });

  it('returns an empty array for a session with no items', async () => {
    const { ddb } = makeFakeDdb({ query: () => ({ Items: [] }) });
    const store = new DynamoContextStore(TABLE, 'us-east-1', ddb as never);
    expect(await store.readAllContextItems('s1')).toEqual([]);
  });
});

describe('getFullTranscript', () => {
  it('returns all 40 turns of a 40-turn session in order — NOT capped at 10', async () => {
    // 40 turns = 80 messages (user + assistant each).
    const messageCount = 80;
    const items = Array.from({ length: messageCount }, (_, i) => itemRow(i, `m${i}`));
    const rows = Array.from({ length: messageCount }, (_, i) =>
      msgRow(`m${i}`, i % 2 === 0 ? 'user' : 'assistant', `message ${i}`),
    );

    const { ddb } = makeFakeDdb({
      query: () => ({ Items: items }),
      batchGet: input => {
        const keys = (input.RequestItems as Record<string, { Keys: Array<{ SK: string }> }>)[TABLE].Keys;
        const wanted = new Set(keys.map(k => k.SK));
        return { Responses: { [TABLE]: rows.filter(r => wanted.has(r.SK)) } };
      },
    });
    const store = new DynamoContextStore(TABLE, 'us-east-1', ddb as never);

    const transcript = await store.getFullTranscript('s1');

    expect(transcript).toHaveLength(messageCount);
    expect(transcript.map(e => e.ordinal)).toEqual(items.map(i => i.ordinal));
    expect(transcript[0].message!.content).toBe('message 0');
    expect(transcript[messageCount - 1].message!.content).toBe(`message ${messageCount - 1}`);
    // Roles alternate, proving hydration matched refs to rows correctly.
    expect(transcript[0].message!.role).toBe('user');
    expect(transcript[1].message!.role).toBe('assistant');
  });

  it('batches message hydration rather than fetching one row at a time', async () => {
    const items = Array.from({ length: 150 }, (_, i) => itemRow(i, `m${i}`));
    const rows = Array.from({ length: 150 }, (_, i) => msgRow(`m${i}`, 'user', `m${i}`));
    const { ddb, sent } = makeFakeDdb({
      query: () => ({ Items: items }),
      batchGet: input => {
        const keys = (input.RequestItems as Record<string, { Keys: Array<{ SK: string }> }>)[TABLE].Keys;
        const wanted = new Set(keys.map(k => k.SK));
        return { Responses: { [TABLE]: rows.filter(r => wanted.has(r.SK)) } };
      },
    });
    const store = new DynamoContextStore(TABLE, 'us-east-1', ddb as never);

    await store.getFullTranscript('s1');

    // 150 messages => 2 BatchGet calls (100-item limit), not 150 Gets.
    expect(sent.filter(s => s.name === 'BatchGetCommand')).toHaveLength(2);
    expect(sent.filter(s => s.name === 'GetCommand')).toHaveLength(0);
  });

  it('hydrates summaries inline at their ordinal, so compacted turns are not lost', async () => {
    const items = [
      itemRow(0, 'sum_1', 'sum'),
      itemRow(1, 'm1'),
      itemRow(2, 'm2'),
    ];
    const { ddb } = makeFakeDdb({
      query: () => ({ Items: items }),
      batchGet: () => ({ Responses: { [TABLE]: [msgRow('m1', 'user', 'later 1'), msgRow('m2', 'assistant', 'later 2')] } }),
      get: () => ({
        Item: {
          depth: 1,
          kind: 'leaf',
          content: 'summary of turns 1-5',
          sourceIds: ['m_old'],
          earliestAt: '2026-08-01T00:00:00Z',
          latestAt: '2026-08-02T00:00:00Z',
          tokens: 50,
        },
      }),
    });
    const store = new DynamoContextStore(TABLE, 'us-east-1', ddb as never);

    const transcript = await store.getFullTranscript('s1');

    expect(transcript).toHaveLength(3);
    expect(transcript[0].type).toBe('sum');
    expect(transcript[0].summary!.content).toBe('summary of turns 1-5');
    expect(transcript[0].message).toBeUndefined();
    expect(transcript[1].type).toBe('msg');
    expect(transcript[1].message!.content).toBe('later 1');
  });

  it('skips a ref whose record has been evicted rather than emitting a hole', async () => {
    const items = [itemRow(0, 'm0'), itemRow(1, 'gone'), itemRow(2, 'm2')];
    const { ddb } = makeFakeDdb({
      query: () => ({ Items: items }),
      // 'gone' is absent from the response — TTL'd or evicted.
      batchGet: () => ({ Responses: { [TABLE]: [msgRow('m0', 'user', 'a'), msgRow('m2', 'user', 'c')] } }),
    });
    const store = new DynamoContextStore(TABLE, 'us-east-1', ddb as never);

    const transcript = await store.getFullTranscript('s1');

    expect(transcript.map(e => e.ref)).toEqual(['m0', 'm2']);
    expect(transcript.every(e => e.message !== undefined)).toBe(true);
  });

  it('returns an empty transcript for an unknown session without hydrating', async () => {
    const { ddb, sent } = makeFakeDdb({ query: () => ({ Items: [] }) });
    const store = new DynamoContextStore(TABLE, 'us-east-1', ddb as never);

    expect(await store.getFullTranscript('nope')).toEqual([]);
    expect(sent.filter(s => s.name === 'BatchGetCommand')).toHaveLength(0);
  });
});
