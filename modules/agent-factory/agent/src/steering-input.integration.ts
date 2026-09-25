/** Targeted live SDK input observations. Never included in unit-test discovery. */
import { mkdtempSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { ClaudeControlAdapter, type AttemptInputChannel, CLAUDE_SDK_VERSION } from './harnesses/claude-control';
import { resilientQuery } from './utils/resilientQuery';
import { buildSteeringText } from './steer-queue';

export async function observeSteeringInput(): Promise<Record<string, unknown>> {
  const cwd = mkdtempSync(join(tmpdir(), 'adp-steering-input-'));
  const adapter = new ClaudeControlAdapter();
  const messages: unknown[] = [];
  const handoffs: Array<{ command_id: string; outcome: string; at: string }> = [];
  const sessions = new Set<string>();
  const channels: AttemptInputChannel[] = [];
  let disposed = 0, queryCloses = 0, assistantMessages = 0, result: unknown = null;
  let nextInstruction = 0, initialized = false;
  const pending: Promise<void>[] = [];
  const commandIds = ['00000000-0000-4000-8000-000000003901', '00000000-0000-4000-8000-000000003902'];
  const attach = adapter.onAttemptHandle();
  const factory = adapter.attemptInputFactory();
  const deliver = () => {
    if (!initialized || nextInstruction >= commandIds.length || !adapter.canAcceptInput()) return;
    const index = nextInstruction++;
    const commandId = commandIds[index];
    pending.push(adapter.submitInput({ kind: 'steering', command_id: commandId,
      text: buildSteeringText(commandId, index === 0
        ? 'Reply FIRST_RECEIVED. Another message follows in this conversation.'
        : 'Reply SECOND_RECEIVED and finish. Do not use tools.'),
    }).then(outcome => { handoffs.push({ command_id: commandId, outcome, at: new Date().toISOString() }); }));
  };
  try {
    for await (const event of resilientQuery({
      queryParams: { prompt: 'Reply READY and wait for two further messages in this conversation. Do not use tools.',
        options: { cwd, permissionMode: 'bypassPermissions', maxTurns: 8, settingSources: [] } },
      maxRetries: 0,
      attemptInputFactory: context => {
        const input = factory(context);
        const iterator = input.input[Symbol.asyncIterator]();
        const observedInput: AsyncIterableIterator<unknown> = {
          [Symbol.asyncIterator]() { return this; },
          async next() {
            const item = await iterator.next();
            if (!item.done) messages.push(item.value);
            return item;
          },
          ...(iterator.return ? { return: iterator.return.bind(iterator) } : {}),
        };
        return { ...input, input: observedInput, dispose: async () => { await input.dispose(); disposed++; } };
      },
      onAttemptHandle: async handle => {
        const session = handle.session as { close(): void };
        const close = session.close.bind(session);
        session.close = () => { close(); queryCloses++; };
        await attach(handle);
        channels.push((adapter as unknown as { activeChannel: AttemptInputChannel }).activeChannel);
        adapter.notifyWhenInputAccepted(deliver);
      },
      cancellation: adapter.cancellationSource(),
      log: () => {},
    })) {
      const message = event as unknown as Record<string, unknown>;
      if (typeof message.session_id === 'string') sessions.add(message.session_id);
      if (message.type === 'system' && message.subtype === 'init') initialized = true;
      if (message.type === 'assistant') assistantMessages++;
      deliver();
      if (message.type === 'result') { result = { subtype: message.subtype, is_error: message.is_error }; break; }
    }
    await Promise.all(pending);
  } finally {
    await adapter.dispose();
    rmSync(cwd, { recursive: true, force: true });
  }
  const later = messages.slice(1) as Array<{ type?: string; message?: { content?: unknown } }>;
  const matched = commandIds.filter(id => later.some(message => typeof message.message?.content === 'string' && message.message.content.includes(id)));
  return {
    sdk_version: CLAUDE_SDK_VERSION,
    initial_task_consumed: messages.length > 0,
    later_user_messages: matched.length,
    message_count: messages.length,
    turn_count: assistantMessages,
    generator_disposed: disposed === 1 && channels.length === 1 && channels.every(channel => channel.isClosed()),
    query_closed: queryCloses > 0,
    attempt_disposals: disposed, query_close_calls: queryCloses, session_ids: [...sessions],
    handoffs, sdk_input_messages: messages, result,
    observed_by: 'Real SDK input iterator consumption, production attempt disposal, and Query.close calls',
  };
}

if (require.main === module) {
  const output = process.argv[process.argv.indexOf('--json') + 1];
  if (!process.argv.includes('--json') || !output) throw new Error('--json output is required');
  observeSteeringInput().then(record => {
    writeFileSync(output, JSON.stringify(record, null, 2) + '\n', { mode: 0o600 });
    const passed = record.initial_task_consumed === true && record.later_user_messages === 2
      && record.generator_disposed === true && record.query_closed === true && Number(record.turn_count) > 0;
    console.log(JSON.stringify({ passed, message_count: record.message_count, later_user_messages: record.later_user_messages }));
    process.exitCode = passed ? 0 : 1;
  }).catch(error => { console.error(error); process.exitCode = 1; });
}
