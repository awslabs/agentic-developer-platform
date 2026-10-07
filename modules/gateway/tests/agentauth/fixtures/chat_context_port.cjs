const dns = require('node:dns');
const path = require('node:path');

const config = JSON.parse(process.argv[2]);
Date.now = () => config.now * 1000;
dns.lookup = (hostname, options, callback) => {
  if (typeof options === 'function') {
    callback = options;
    options = {};
  }
  if (hostname !== 'chat-gateway.test') throw new Error('Unexpected integration-test destination');
  process.nextTick(() => options?.all
    ? callback(null, [{ address: '127.0.0.1', family: 4 }])
    : callback(null, '127.0.0.1', 4));
};

const { ChatDataClient } = require(path.join(config.agent, 'src/complex-task-chat/gateway/chat-data-client.ts'));
const { GatewayHistoryStore } = require(path.join(config.agent, 'src/complex-task-chat/context/store/gateway-history.ts'));
const { GatewayContextManager } = require(path.join(config.agent, 'src/complex-task-chat/context/gateway-context.ts'));
const { DEFAULT_LCM_CONFIG } = require(path.join(config.agent, 'src/complex-task-chat/context/lcm/config.ts'));
const client = new ChatDataClient({ baseUrl: config.url, allowHttp: true, workloadToken: async () => 'chat-token' });
const history = new GatewayHistoryStore(client);
const summarizations = [];
const context = new GatewayContextManager(client, {
  summarize: async input => {
    if (config.mode !== 'compact') throw new Error('Unexpected compaction');
    summarizations.push(input);
    return 'Brief.';
  },
}, config.mode === 'compact'
  ? { ...DEFAULT_LCM_CONFIG, freshTailCount: 1, leafChunkTokens: 1, leafTargetTokens: 2 }
  : DEFAULT_LCM_CONFIG);

async function failure(operation) {
  try {
    await operation();
    return { error: null };
  } catch (error) {
    return { error: error.code, status: error.status };
  }
}

async function main() {
  const session = config.session ?? 'session-a';
  const input = { sessionId: session, userMessage: config.message, tokenBudget: 10_000 };
  if (config.mode === 'assemble') return context.assemble(input);
  if (config.mode === 'denied') return failure(() => history.acceptedUserTurn(session));
  if (config.mode === 'read') return history.readPage(session, { limit: 1 });
  if (config.mode === 'probe') return {
    messages: await failure(() => history.getMessagesByIds(session, config.ids)),
    summary: await failure(() => history.getSummaryById(session, config.summaryId)),
    expansion: await failure(() => context.tools()[0].handler({ summary_id: config.summaryId })),
    transcript: await failure(() => history.getFullTranscript(session)),
    ...(config.cursor ? { cursor: await failure(() => history.readPage(session, { limit: 1, cursor: config.cursor })) } : {}),
    ...(config.otherSession ? { otherSession: await failure(() => history.readPage(config.otherSession)) } : {}),
  };
  if (config.mode === 'revoke') {
    const first = await history.readPage(session, { limit: 1 });
    return { first, next: await failure(() => history.readPage(session, { limit: 1, cursor: first.next_cursor })) };
  }
  if (config.mode === 'compact') {
    const firstPage = await history.readPage(session);
    const secondPage = await history.readPage(session, { cursor: firstPage.next_cursor });
    const before = await history.readSnapshot(session);
    const assembled = await context.assemble(input);
    const turn = {
      sessionId: session, userMessage: { role: 'user', content: config.message },
      assistantMessage: { role: 'assistant', content: 'I inspected the history.' },
    };
    await context.record(turn);
    const after = await history.readSnapshot(session);
    await context.record(turn);
    const replay = await history.readSnapshot(session);
    const summaryId = after.items.find(item => item.type === 'sum').ref;
    const summary = await history.getSummaryById(session, summaryId);
    const sources = await history.getMessagesByIds(session, summary.sourceIds);
    const expanded = await context.tools()[0].handler({ summary_id: summaryId });
    const staleCompaction = await failure(() => history.compact(session, {
      idempotency_key: 'stale-compaction', expected_version: before.version,
      from_ordinal: 1, to_ordinal: 94, content: 'Brief.', tokens: 2, source_ids: summary.sourceIds,
    }));
    const staleCursor = await failure(() => history.readPage(session, { cursor: firstPage.next_cursor }));
    const standaloneWrite = {
      idempotency_key: 'standalone-summary', expected_version: after.version,
      content: 'Standalone brief.', tokens: 5, source_ids: summary.sourceIds.slice(0, 2),
    };
    const standalone = await history.appendSummary(session, standaloneWrite);
    const standaloneReplay = await history.appendSummary(session, standaloneWrite);
    const summaryConflict = await failure(() => history.appendSummary(session, { ...standaloneWrite, content: 'Changed summary' }));
    return {
      firstPage, secondPage, before, assembled, after, replay, summaryId, summary, sources, expanded,
      summarizations, staleCompaction, staleCursor, standalone, standaloneReplay, summaryConflict,
      transcript: await history.getFullTranscript(session), cursorPage: await history.readPage(session, { limit: 1 }),
    };
  }
  const before = await context.assemble(input);
  const turn = {
    sessionId: session, userMessage: { role: 'user', content: config.message },
    assistantMessage: { role: 'assistant', content: 'I inspected the attachment.' },
  };
  await context.record(turn);
  const first = await history.getFullTranscript(session);
  await context.record(turn);
  const transcript = await history.getFullTranscript(session);
  const accepted = await history.acceptedUserTurn(session);
  const snapshot = await history.readSnapshot(session);
  const write = {
    user_turn_id: accepted.message_id, idempotency_key: 'new-write', expected_version: snapshot.version,
    content: 'Another response', tokens: 4,
  };
  const wrongTurn = await failure(() => history.appendAssistant(session, { ...write, user_turn_id: 'guessed-user-turn' }));
  const stale = await failure(() => history.appendAssistant(session, { ...write, expected_version: 0 }));
  return { before, first, transcript, accepted, version: snapshot.version, wrongTurn, stale };
}

main().then(result => process.stdout.write(JSON.stringify(result))).catch(error => {
  process.stderr.write(JSON.stringify({ error: error.code ?? error.name, status: error.status }));
  process.exitCode = 1;
});
