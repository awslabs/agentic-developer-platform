import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer, ServerResponse } from 'node:http';
import { AddressInfo } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';
import { CREW_URL } from './crew-kirocrew';
import { CrewExecutorDownError, CrewSessionGateway, crewSessionForAdpSession, executeKiroCrewTurn } from './crew-kirocrew-bridge';

describe('retained Crew session mapping', () => {
  let home: string;
  let mapDirectory: string;
  beforeEach(async () => {
    home = await mkdtemp(join(tmpdir(), 'adp-crew-map-'));
    mapDirectory = join(home, 'adp-crew-session-map');
  });
  afterEach(async () => { await rm(home, { force: true, recursive: true }); });

  it('creates only once, reuses a retained mapping and separates ADP sessions', async () => {
    const create = jest.fn().mockResolvedValueOnce('crew-one').mockResolvedValueOnce('crew-two');
    expect(await crewSessionForAdpSession(home, mapDirectory, 'adp-one', create)).toBe('crew-one');
    expect(await crewSessionForAdpSession(home, mapDirectory, 'adp-one', create)).toBe('crew-one');
    expect(await crewSessionForAdpSession(home, mapDirectory, 'adp-two', create)).toBe('crew-two');
    expect(create).toHaveBeenCalledTimes(2);
    expect(await readdir(mapDirectory)).toHaveLength(2);
  });

  it('does not persist failed session creation', async () => {
    await expect(crewSessionForAdpSession(home, mapDirectory, 'adp-one', async () => {
      throw new Error('gateway unavailable');
    })).rejects.toThrow('gateway unavailable');
    expect(await crewSessionForAdpSession(home, mapDirectory, 'adp-one', async () => 'recovered')).toBe('recovered');
  });

  it('rejects an outside or symlinked map and corrupted retained entries', async () => {
    await expect(crewSessionForAdpSession(home, join(home, '..', 'outside'), 'adp-one', async () => 'x'))
      .rejects.toThrow('retained home');
    const outside = await mkdtemp(join(tmpdir(), 'adp-map-outside-'));
    try {
      await symlink(outside, mapDirectory);
      await expect(crewSessionForAdpSession(home, mapDirectory, 'adp-one', async () => 'x'))
        .rejects.toThrow('symlink');
    } finally { await rm(mapDirectory, { force: true }); await rm(outside, { recursive: true, force: true }); }
    await crewSessionForAdpSession(home, mapDirectory, 'adp-one', async () => 'crew-one');
    const path = join(mapDirectory, createHash('sha256').update('adp-one').digest('hex') + '.json');
    await writeFile(path, JSON.stringify({ sessionId: 'adp-two', target: 'crew-one' }));
    await expect(crewSessionForAdpSession(home, mapDirectory, 'adp-one', async () => 'x'))
      .rejects.toThrow('Invalid Crew session map');
  });
});

describe('Crew session-control transport', () => {
  let home: string;
  beforeEach(async () => {
    home = await mkdtemp(join(tmpdir(), 'adp-crew-api-'));
    await mkdir(join(home, 'run'));
    await writeFile(join(home, 'run', 'gateway-5476.secret'), 'fake-secret\n', { mode: 0o600 });
  });
  afterEach(async () => { await rm(home, { force: true, recursive: true }); });

  it.each([0, 1, 2])('reconciles all assistant rows with %i streamed rows and no duplicate delivery', async streamedCount => {
    const replies = ['Intermediate response. ', 'Final answer.'];
    let sent = false;
    const gateway = new CrewSessionGateway(home);
    jest.spyOn(gateway, 'createSession').mockResolvedValue('crew-one');
    jest.spyOn(gateway, 'send').mockImplementation(async () => { sent = true; });
    jest.spyOn(gateway, 'read').mockImplementation(async () => ({ running: false, queue_depth: 0,
      total: sent ? 3 : 0, messages: sent ? replies.map(content => ({ role: 'assistant', content })) : [] }));
    jest.spyOn(gateway, 'stream').mockImplementation(async () => new Response(new ReadableStream({
      start(controller) {
        const frames = replies.slice(0, streamedCount).map((content, index) => {
          const frame = `event: chat_message\ndata: ${JSON.stringify({ slot: 'crew-one', role: 'assistant', content,
            meta: { mid: `row-${index}` } })}\n\n`;
          return frame + frame;
        }).join('');
        if (frames) controller.enqueue(new TextEncoder().encode(frames));
      },
    })));
    const publishCrewTextEvent = jest.fn();
    const record = jest.fn();
    const beforeReceipt = jest.fn(async () => {
      expect(record).not.toHaveBeenCalled();
      expect(publishCrewTextEvent.mock.calls.at(-1)?.[2]).toBe('end');
    });
    const prepared = { client: { sessionScope: async () => ({ session_id: 'adp-one' }), publishCrewTextEvent },
      context: { userMessage: 'Reply' }, turn: { ref: 'user_one' }, data: { record }, beforeReceipt } as unknown as Parameters<typeof executeKiroCrewTurn>[0];
    await executeKiroCrewTurn(prepared, home, join(home, 'adp-crew-session-map'), gateway);
    expect(publishCrewTextEvent.mock.calls.filter(call => call[2] === 'content').map(call => call[3]).join('')).toBe(replies.join(''));
    expect(publishCrewTextEvent.mock.calls.filter(call => call[2] === 'start')).toHaveLength(1);
    expect(publishCrewTextEvent.mock.calls.filter(call => call[2] === 'end')).toHaveLength(1);
    expect(record).toHaveBeenCalledTimes(1);
    expect(record).toHaveBeenCalledWith(replies.join(''), 'row');
    expect(beforeReceipt).toHaveBeenCalledTimes(1);
    jest.restoreAllMocks();
  });

  it('does not submit a successful receipt when pre-receipt consolidation fails', async () => {
    const gateway = new CrewSessionGateway(home);
    jest.spyOn(gateway, 'createSession').mockResolvedValue('crew-one');
    jest.spyOn(gateway, 'send').mockResolvedValue();
    jest.spyOn(gateway, 'read').mockResolvedValue({ running: false, queue_depth: 0, total: 2,
      messages: [{ role: 'assistant', content: 'Reply' }] });
    jest.spyOn(gateway, 'stream').mockResolvedValue(new Response(new ReadableStream()));
    const record = jest.fn();
    const prepared = { client: { sessionScope: async () => ({ session_id: 'adp-one' }), publishCrewTextEvent: jest.fn() },
      context: { userMessage: 'Reply' }, turn: { ref: 'user_one' }, data: { record },
      beforeReceipt: async () => { throw new Error('Consolidation failed'); } } as unknown as Parameters<typeof executeKiroCrewTurn>[0];
    await expect(executeKiroCrewTurn(prepared, home, join(home, 'adp-crew-session-map'), gateway)).rejects.toThrow('Consolidation failed');
    expect(record).not.toHaveBeenCalled();
    jest.restoreAllMocks();
  });

  it.each(['complete', 'cancel', 'disconnect'])('settles real HTTP SSE reads on %s without an unhandled rejection', async mode => {
    const cancellation = new AbortController();
    const unhandled = jest.fn();
    process.on('unhandledRejection', unhandled);
    let sent = false;
    let streamResponse: ServerResponse | undefined;
    const server = createServer((request, response) => {
      if (request.url?.startsWith('/api/stream')) {
        streamResponse = response;
        response.writeHead(200, { 'content-type': 'text/event-stream' });
        response.write(`event: chat_message\ndata: ${JSON.stringify({ slot: 'crew-one', role: 'assistant', content: 'Reply',
          meta: { mid: 'row-one' } })}\n\n`);
        return;
      }
      response.setHeader('content-type', 'application/json');
      if (request.url?.startsWith('/api/session-control/read')) {
        response.end(JSON.stringify({ ok: true, target: 'crew-one', running: false, queue_depth: 0, total: sent ? 2 : 0,
          messages: sent ? [{ role: 'assistant', content: 'Reply' }] : [] }));
      } else {
        sent = true;
        response.end(JSON.stringify({ ok: true, target: 'crew-one' }));
      }
    });
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    try {
      await crewSessionForAdpSession(home, join(home, 'adp-crew-session-map'), 'adp-one', async () => 'crew-one');
      const address = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
      const gateway = new CrewSessionGateway(home, (url, init) => fetch(String(url).replace(CREW_URL, address), init), async () => 'a'.repeat(16));
      const record = jest.fn();
      const publishCrewTextEvent = jest.fn(async (_operation: string, _sequence: number, event: string) => {
        if (event !== 'content') return;
        if (mode === 'cancel') cancellation.abort();
        if (mode === 'disconnect') streamResponse!.destroy();
        await delay(25);
      });
      const prepared = { client: { sessionScope: async () => ({ session_id: 'adp-one' }), publishCrewTextEvent },
        context: { userMessage: 'Reply' }, turn: { ref: 'user_one' }, data: { record } } as unknown as Parameters<typeof executeKiroCrewTurn>[0];
      const execution = executeKiroCrewTurn(prepared, home, join(home, 'adp-crew-session-map'), gateway, cancellation.signal);
      if (mode === 'complete') {
        await expect(execution).resolves.toBeUndefined();
        expect(record).toHaveBeenCalledWith('Reply', 'row');
      } else {
        await expect(execution).rejects.toBeInstanceOf(mode === 'cancel' ? DOMException : CrewExecutorDownError);
        expect(record).not.toHaveBeenCalled();
      }
      await delay(25);
      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      server.closeAllConnections();
      await new Promise<void>(resolve => server.close(() => resolve()));
      process.removeListener('unhandledRejection', unhandled);
    }
  });

  it('bootstraps a stable control slot and calls session create and send with the internal credential', async () => {
    const controlName = `adp-control-${createHash('sha256').update('adp-one').digest('hex').slice(0, 24)}`;
    const request = jest.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      if (_url === `${CREW_URL}/api/chat/slots`) return Response.json({ key: controlName });
      if (_url === `${CREW_URL}/api/session-control/create`) return Response.json({ ok: true, target: 'crew-one' });
      return Response.json({ ok: true, target: 'crew-one', started: true });
    });
    const gateway = new CrewSessionGateway(home, request);
    expect(await gateway.createSession('adp-one')).toBe('crew-one');
    await gateway.send('adp-one', 'crew-one', 'Hello');
    expect(request).toHaveBeenCalledTimes(3);
    expect(request.mock.calls.map(call => call[0])).toEqual([
      `${CREW_URL}/api/chat/slots`, `${CREW_URL}/api/session-control/create`, `${CREW_URL}/api/session-control/send`,
    ]);
    expect(request.mock.calls[1][1]).toMatchObject({
      headers: { 'X-Internal-Secret': 'fake-secret', 'X-Session-Key': controlName },
      body: JSON.stringify({ title: 'ADP conversation' }),
    });
    expect(request.mock.calls[2][1]).toMatchObject({
      body: JSON.stringify({ target: 'crew-one', message: 'Hello', steer: false }),
    });
  });

  it('refuses create errors without storing credentials or accepting a forged caller', async () => {
    const request = jest.fn(async () => Response.json({ code: 'forbidden' }, { status: 403 }));
    await expect(new CrewSessionGateway(home, request).createSession('adp-one'))
      .rejects.toThrow('Crew session control unavailable (403)');
    await rm(join(home, 'run', 'gateway-5476.secret'));
    await expect(new CrewSessionGateway(home, request).createSession('adp-one')).rejects.toThrow();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('sends the accepted turn, relays SSE text into AG-UI and submits the recorded receipt', async () => {
    const controlName = `adp-control-${createHash('sha256').update('adp-one').digest('hex').slice(0, 24)}`;
    let sent = false;
    const request = jest.fn(async (url: string | URL | Request) => {
      if (url === `${CREW_URL}/api/chat/slots`) return Response.json({ key: controlName });
      if (url === `${CREW_URL}/api/session-control/create`) return Response.json({ ok: true, target: 'crew-one' });
      if (url === `${CREW_URL}/api/stream?token=${'a'.repeat(16)}`) {
        const body = new ReadableStream({ start(controller) {
          const frame = 'event: chat_message\ndata: {"slot":"crew-one","role":"assistant","content":"KIROCREW-ADP-OK","meta":{"mid":"mid-one"}}\n\n';
          controller.enqueue(new TextEncoder().encode('event: chat_message\ndata: {"slot":"wrong-turn","role":"assistant","content":"Wrong"}\n\n' + frame + frame));
        } });
        return new Response(body, { headers: { 'content-type': 'text/event-stream' } });
      }
      if (String(url).includes('/api/session-control/read')) return Response.json({ ok: true, target: 'crew-one',
        running: false, queue_depth: 0, total: sent ? 2 : 0,
        messages: sent ? [{ role: 'user', content: 'Reply with exactly: KIROCREW-ADP-OK' },
          { role: 'assistant', content: 'KIROCREW-ADP-OK' }] : [] });
      sent = true;
      return Response.json({ ok: true, target: 'crew-one', started: true });
    });
    const gateway = new CrewSessionGateway(home, request, async () => 'a'.repeat(16));
    const publishCrewTextEvent = jest.fn(async (_operationId: string, _sequence: number,
      _eventType: string, _delta?: string) => undefined);
    const submitTurnResult = jest.fn(async (_result: object) => undefined);
    const record = jest.fn(async (_reply: string, granularity: string) => submitTurnResult({
      outcome: 'completed', message_id: 'msg-one', reply_granularity: granularity,
    }));
    const prepared = { client: { sessionScope: async () => ({ session_id: 'adp-one', run_id: 'run-one' }),
      publishCrewTextEvent, submitTurnResult }, context: { userMessage: 'Reply with exactly: KIROCREW-ADP-OK' },
      turn: { ref: 'user_' + 'a'.repeat(64) }, data: { record } } as unknown as Parameters<typeof executeKiroCrewTurn>[0];
    await executeKiroCrewTurn(prepared, home, join(home, 'adp-crew-session-map'), gateway);
    expect(publishCrewTextEvent.mock.calls.map(call => call.slice(1))).toEqual([
      [0, 'start'], [2, 'content', 'KIROCREW-ADP-OK'], [3, 'end'],
    ]);
    expect(record).toHaveBeenCalledWith('KIROCREW-ADP-OK', 'row');
    expect(submitTurnResult).toHaveBeenCalledWith({ outcome: 'completed', message_id: 'msg-one', reply_granularity: 'row' });
    expect(request.mock.calls.filter(call => call[0] === `${CREW_URL}/api/session-control/send`)).toHaveLength(1);
  });

  it('fails a turn without publishing a fallback reply when the gateway stream closes mid-turn', async () => {
    const controlName = `adp-control-${createHash('sha256').update('adp-one').digest('hex').slice(0, 24)}`;
    let sent = false;
    const request = jest.fn(async (url: string | URL | Request) => {
      if (url === `${CREW_URL}/api/chat/slots`) return Response.json({ key: controlName });
      if (url === `${CREW_URL}/api/session-control/create`) return Response.json({ ok: true, target: 'crew-one' });
      if (String(url).includes('/api/stream?token=')) return new Response(new ReadableStream({
        start(controller) { controller.close(); },
      }), { headers: { 'content-type': 'text/event-stream' } });
      if (String(url).includes('/api/session-control/read')) return Response.json({ ok: true, target: 'crew-one',
        running: sent, queue_depth: 0, total: 0, messages: [] });
      sent = true;
      return Response.json({ ok: true, target: 'crew-one', started: true });
    });
    const gateway = new CrewSessionGateway(home, request, async () => 'a'.repeat(16));
    const publishCrewTextEvent = jest.fn();
    const record = jest.fn();
    const prepared = { client: { sessionScope: async () => ({ session_id: 'adp-one' }), publishCrewTextEvent },
      context: { userMessage: 'Reply with exactly: KIROCREW-ADP-OK' }, turn: { ref: 'user_' + 'a'.repeat(64) },
      data: { record } } as unknown as Parameters<typeof executeKiroCrewTurn>[0];
    await expect(executeKiroCrewTurn(prepared, home, join(home, 'adp-crew-session-map'), gateway))
      .rejects.toBeInstanceOf(CrewExecutorDownError);
    expect(request.mock.calls.filter(call => call[0] === `${CREW_URL}/api/session-control/send`)).toHaveLength(1);
    expect(publishCrewTextEvent).not.toHaveBeenCalled();
    expect(record).not.toHaveBeenCalled();
  });

  it('keeps two sequential ADP turns in the same Crew session with separate event streams', async () => {
    const controlName = `adp-control-${createHash('sha256').update('adp-one').digest('hex').slice(0, 24)}`;
    let sent = 0;
    const request = jest.fn(async (url: string | URL | Request) => {
      if (url === `${CREW_URL}/api/chat/slots`) return Response.json({ key: controlName });
      if (url === `${CREW_URL}/api/session-control/create`) return Response.json({ ok: true, target: 'crew-one' });
      if (String(url).includes('/api/stream?token=')) {
        const content = sent === 0 ? 'First reply' : 'Second reply';
        const frame = `event: chat_message\ndata: ${JSON.stringify({ slot: 'crew-one', role: 'assistant', content,
          meta: { mid: `mid-${sent + 1}` } })}\n\n`;
        return new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(frame)); } }),
          { headers: { 'content-type': 'text/event-stream' } });
      }
      if (String(url).includes('/api/session-control/read')) return Response.json({ ok: true, target: 'crew-one',
        running: false, queue_depth: 0, total: sent * 2, messages: sent ? [{ role: 'assistant',
          content: sent === 1 ? 'First reply' : 'Second reply' }] : [] });
      sent++;
      return Response.json({ ok: true, target: 'crew-one', started: true });
    });
    const gateway = new CrewSessionGateway(home, request, async () => 'a'.repeat(16));
    const publishCrewTextEvent = jest.fn(async (_operationId: string, _sequence: number,
      _eventType: string, _delta?: string) => undefined);
    const record = jest.fn(async (_reply: string) => undefined);
    for (const [ref, message] of [['a', 'first'], ['b', 'second']]) {
      const prepared = { client: { sessionScope: async () => ({ session_id: 'adp-one', run_id: 'run-one' }),
        publishCrewTextEvent }, context: { userMessage: message }, turn: { ref: 'user_' + ref.repeat(64) },
      data: { record } } as unknown as Parameters<typeof executeKiroCrewTurn>[0];
      await executeKiroCrewTurn(prepared, home, join(home, 'adp-crew-session-map'), gateway);
    }
    expect(request.mock.calls.filter(call => call[0] === `${CREW_URL}/api/session-control/create`)).toHaveLength(1);
    expect(record.mock.calls).toEqual([['First reply', 'row'], ['Second reply', 'row']]);
    expect(publishCrewTextEvent.mock.calls.filter(call => call[2] === 'content').map(call => [call[0], call[3]]))
      .toEqual([[expect.stringMatching(/^crew_/), 'First reply'], [expect.stringMatching(/^crew_/), 'Second reply']]);
    expect(publishCrewTextEvent.mock.calls[1][0]).not.toBe(publishCrewTextEvent.mock.calls[4][0]);
  });

  it('steers only a busy Crew turn and rejects a requeued steer', async () => {
    let running = false;
    const controlName = `adp-control-${createHash('sha256').update('adp-one').digest('hex').slice(0, 24)}`;
    const request = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
      if (String(url).includes('/api/session-control/read')) return Response.json({ ok: true, target: 'crew-one',
        running, queue_depth: 0, total: 0, messages: [] });
      if (url === `${CREW_URL}/api/session-control/send`) return Response.json({ ok: true, target: 'crew-one',
        steered: JSON.parse(init!.body as string).message === 'Good direction' });
      return Response.json({ key: controlName });
    });
    const gateway = new CrewSessionGateway(home, request);
    await expect(gateway.steer('adp-one', 'crew-one', 'Too early')).rejects.toThrow('running turn');
    running = true;
    await gateway.steer('adp-one', 'crew-one', 'Good direction');
    await expect(gateway.steer('adp-one', 'crew-one', 'Requeued')).rejects.toThrow('not applied');
    expect(request.mock.calls.filter(call => call[0] === `${CREW_URL}/api/session-control/send`)
      .map(call => JSON.parse(call[1]!.body as string).steer)).toEqual([true, true]);
  });
});
