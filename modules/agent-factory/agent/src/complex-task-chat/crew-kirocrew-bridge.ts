import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { mkdir, open, realpath, rename, rm } from 'node:fs/promises';
import { basename, dirname, join, resolve } from 'node:path';
import { CREW_URL } from './crew-kirocrew';
import { mintCrewLink } from './crew-dashboard-custody';
import { ChatDataClient } from './gateway/chat-data-client';
import { SandboxDataRuntime } from './sandbox-data';

/** Mailbox turns arrive only after the preceding receipt. Steering therefore runs as a separately receipted next turn;
 * delivering a steer into an active Crew turn requires a future authorized mailbox path. */

export class CrewExecutorDownError extends Error {}

export class CrewSessionGateway {
  constructor(private readonly home: string, private readonly request: typeof fetch = fetch,
    private readonly mint: (home: string) => Promise<string> = mintCrewLink) {}

  private controlName(adpSessionId: string): string {
    return `adp-control-${createHash('sha256').update(adpSessionId).digest('hex').slice(0, 24)}`;
  }

  private async call(path: string, body?: object, sessionKey?: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const handle = await open(join(this.home, 'run', 'gateway-5476.secret'), constants.O_RDONLY | constants.O_NOFOLLOW);
    let secret: string;
    try { secret = (await handle.readFile('utf8')).trim(); }
    finally { await handle.close(); }
    if (!secret || secret.length > 4096) throw new Error('Crew internal secret unavailable');
    let response: Response;
    try {
      response = await this.request(`${CREW_URL}${path}`, {
        method: body ? 'POST' : 'GET', headers: { 'X-Internal-Secret': secret, ...(sessionKey ? { 'X-Session-Key': sessionKey } : {}),
          ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), signal,
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new CrewExecutorDownError('Crew session gateway disconnected');
    }
    if (!response.ok) {
      await response.body?.cancel();
      if (response.status >= 500) throw new CrewExecutorDownError('Crew session gateway unavailable');
      throw new Error(`Crew session control unavailable (${response.status})`);
    }
    const value: unknown = await response.json();
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid Crew session response');
    return value as Record<string, unknown>;
  }

  async createSession(adpSessionId: string, signal?: AbortSignal): Promise<string> {
    const controlName = this.controlName(adpSessionId);
    const control = await this.call('/api/chat/slots', { name: controlName }, undefined, signal);
    if (control.key !== controlName) throw new Error('Crew control session mismatch');
    const created = await this.call('/api/session-control/create', { title: 'ADP conversation' }, controlName, signal);
    if (created.ok !== true || typeof created.target !== 'string' || !created.target) {
      throw new Error('Crew session creation failed');
    }
    return created.target;
  }

  async send(adpSessionId: string, target: string, message: string, signal?: AbortSignal): Promise<void> {
    const result = await this.call('/api/session-control/send', { target, message, steer: false }, this.controlName(adpSessionId), signal);
    if (result.ok !== true || result.target !== target) throw new Error('Crew delivery failed');
  }

  async steer(adpSessionId: string, target: string, message: string, signal?: AbortSignal): Promise<void> {
    if (!(await this.read(adpSessionId, target, undefined, signal)).running) throw new Error('Crew steer requires a running turn');
    const result = await this.call('/api/session-control/send', { target, message, steer: true },
      this.controlName(adpSessionId), signal);
    if (result.ok !== true || result.target !== target || result.steered !== true) {
      throw new Error('Crew steer was not applied to the running turn');
    }
  }

  async read(adpSessionId: string, target: string, since?: number, signal?: AbortSignal): Promise<{
    running: boolean; queue_depth: number; total: number; messages: Array<{ role: string; content: string }>;
  }> {
    const value = await this.call(`/api/session-control/read?target=${encodeURIComponent(target)}${since === undefined ? '' : `&since=${since}`}`,
      undefined, this.controlName(adpSessionId), signal);
    if (value.ok !== true || value.target !== target || typeof value.running !== 'boolean' ||
        typeof value.queue_depth !== 'number' || !Number.isSafeInteger(value.total) || !Array.isArray(value.messages) ||
        value.messages.some(message => !message || typeof message.role !== 'string' || typeof message.content !== 'string')) {
      throw new Error('Invalid Crew transcript');
    }
    return value as { running: boolean; queue_depth: number; total: number; messages: Array<{ role: string; content: string }> };
  }

  async stream(signal?: AbortSignal): Promise<Response> {
    const token = await this.mint(this.home);
    if (!/^[A-Za-z0-9_.-]{16,4096}$/.test(token)) throw new Error('Crew stream token unavailable');
    let response: Response;
    try {
      response = await this.request(`${CREW_URL}/api/stream?token=${encodeURIComponent(token)}`,
        { headers: { Accept: 'text/event-stream' }, signal });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new CrewExecutorDownError('Crew stream disconnected');
    }
    if (!response.ok || !response.body || response.headers.get('content-type')?.split(';')[0] !== 'text/event-stream') {
      await response.body?.cancel();
      if (response.status >= 500) throw new CrewExecutorDownError('Crew stream unavailable');
      throw new Error('Crew stream unavailable');
    }
    return response;
  }
}

type PreparedCrewTurn = {
  client: ChatDataClient;
  data: SandboxDataRuntime;
  turn: { ref: string };
  context: Awaited<ReturnType<SandboxDataRuntime['prepare']>>;
  beforeReceipt?: () => Promise<void>;
};

export async function executeKiroCrewTurn(prepared: PreparedCrewTurn, home: string, mapDirectory: string,
  gateway = new CrewSessionGateway(home), signal?: AbortSignal): Promise<void> {
  const { client, context, data } = prepared;
  const { session_id: sessionId } = await client.sessionScope();
  const target = await crewSessionForAdpSession(home, mapDirectory, sessionId, () => gateway.createSession(sessionId, signal));
  const baseline = await gateway.read(sessionId, target, undefined, signal);
  if (baseline.running || baseline.queue_depth) throw new Error('Crew session already busy');
  const streamAbort = new AbortController();
  const streamSignal = signal ? AbortSignal.any([signal, streamAbort.signal]) : streamAbort.signal;
  const stream = await gateway.stream(streamSignal);
  const reader = stream.body!.getReader();
  let streamFailure: CrewExecutorDownError | undefined;
  const readFrame = () => reader.read().then(value => ({ value }), error => {
    streamFailure = new CrewExecutorDownError('Crew stream disconnected during turn');
    return { error };
  });
  let pending = readFrame();
  let buffer = '';
  let text = '';
  let sequence = 0;
  const seenMessages = new Map<string, string>();
  const operationId = `crew_${createHash('sha256').update(JSON.stringify([sessionId, prepared.turn.ref])).digest('hex')}`;
  const decoder = new TextDecoder();
  let idleTimer = setTimeout(() => streamAbort.abort(new Error('Crew stream idle timeout')), 60_000);
  try {
    await gateway.send(sessionId, target, context.userMessage, signal);
    for (;;) {
      signal?.throwIfAborted();
      if (streamAbort.signal.aborted) throw new CrewExecutorDownError('Crew stream idle timeout');
      const frame = await Promise.race([pending,
        new Promise<undefined>(resolve => setTimeout(() => resolve(undefined), 250))]);
      signal?.throwIfAborted();
      if (streamFailure) throw streamFailure;
      if (frame && 'value' in frame) {
        if (frame.value.done) throw new CrewExecutorDownError('Crew stream closed during turn');
        clearTimeout(idleTimer);
        idleTimer = setTimeout(() => streamAbort.abort(new Error('Crew stream idle timeout')), 60_000);
        buffer += decoder.decode(frame.value.value, { stream: true }).replace(/\r\n/g, '\n');
        if (buffer.length > 65_536) throw new Error('Crew stream frame too large');
        pending = readFrame();
        let end: number;
        while ((end = buffer.indexOf('\n\n')) !== -1) {
          const raw = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          if (!raw.startsWith('event: chat_message\n')) continue;
          const dataLine = raw.split('\n').find(line => line.startsWith('data: '));
          if (!dataLine) continue;
          const message: unknown = JSON.parse(dataLine.slice(6));
          if (!message || typeof message !== 'object') continue;
          const row = message as { slot?: unknown; role?: unknown; content?: unknown; meta?: { mid?: unknown } };
          if (row.slot !== target || row.role !== 'assistant' || typeof row.content !== 'string' || !row.content) continue;
          const mid = row.meta?.mid;
          if (typeof mid !== 'string' || !mid) throw new Error('Crew reply frame missing delivery identity');
          if (seenMessages.has(mid)) {
            if (seenMessages.get(mid) !== row.content) throw new Error('Crew reply frame identity changed');
            continue;
          }
          seenMessages.set(mid, row.content);
          if (Buffer.byteLength(text + row.content) > 65_536) throw new Error('Crew reply exceeds the ADP text bound');
          if (!text && sequence === 0) { await client.publishCrewTextEvent(operationId, sequence, 'start'); sequence += 2; }
          for (let offset = 0; offset < row.content.length; offset += 1024) {
            await client.publishCrewTextEvent(operationId, sequence++, 'content', row.content.slice(offset, offset + 1024));
          }
          text += row.content;
        }
      }
      const state = await gateway.read(sessionId, target, baseline.total, signal);
      signal?.throwIfAborted();
      if (streamFailure) throw streamFailure;
      if (!state.running && !state.queue_depth) {
        const replies = state.messages.filter(message => message.role === 'assistant' && message.content).map(message => message.content);
        const reply = replies.join('');
        if (!reply) throw new Error('Crew turn completed without a reply');
        if ([...seenMessages.values()].some((content, index) => replies[index] !== content)) {
          throw new Error('Crew reply does not match streamed text');
        }
        if (Buffer.byteLength(reply) > 65_536) throw new Error('Crew reply exceeds the ADP text bound');
        const remaining = reply.slice(text.length);
        if (!text) { await client.publishCrewTextEvent(operationId, sequence, 'start'); sequence += 2; }
        for (let offset = 0; offset < remaining.length; offset += 1024) {
          await client.publishCrewTextEvent(operationId, sequence++, 'content', remaining.slice(offset, offset + 1024));
        }
        await client.publishCrewTextEvent(operationId, sequence, 'end');
        signal?.throwIfAborted();
        if (streamFailure) throw streamFailure;
        await prepared.beforeReceipt?.();
        signal?.throwIfAborted();
        await data.record(reply, 'row');
        return;
      }
    }
  } finally {
    clearTimeout(idleTimer);
    streamAbort.abort();
    await reader.cancel().catch(() => undefined);
    await pending;
  }
}

export async function crewSessionForAdpSession(
  home: string, mapDirectory: string, sessionId: string, create: () => Promise<string>,
): Promise<string> {
  if (!sessionId || !mapDirectory || resolve(mapDirectory) !== mapDirectory ||
      dirname(mapDirectory) !== resolve(home) || basename(mapDirectory) !== 'adp-crew-session-map') {
    throw new Error('Crew session map must be inside the retained home');
  }
  await mkdir(mapDirectory, { recursive: true, mode: 0o700 });
  if (await realpath(mapDirectory) !== join(await realpath(home), 'adp-crew-session-map')) {
    throw new Error('Crew session map must not be a symlink');
  }
  const path = join(mapDirectory, createHash('sha256').update(sessionId).digest('hex') + '.json');
  async function readMapping(): Promise<string | undefined> {
    let handle;
    try { handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
      throw error;
    }
    try {
      const mapping: unknown = JSON.parse(await handle.readFile('utf8'));
      if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping) ||
          (mapping as { sessionId?: unknown }).sessionId !== sessionId ||
          typeof (mapping as { target?: unknown }).target !== 'string' ||
          !(mapping as { target: string }).target.trim()) throw new Error('Invalid Crew session map');
      return (mapping as { target: string }).target;
    } finally { await handle.close(); }
  }
  const existing = await readMapping();
  if (existing) return existing;
  const target = await create();
  if (!target || !target.trim()) throw new Error('Crew session creation returned no target');
  const temporary = `${path}.${process.pid}.${createHash('sha256').update(target).digest('hex')}.tmp`;
  const handle = await open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    await handle.writeFile(JSON.stringify({ sessionId, target }) + '\n');
    await handle.sync();
  } finally { await handle.close(); }
  try { await rename(temporary, path); }
  catch (error) { await rm(temporary, { force: true }); throw error; }
  return target;
}
