import { ChatDataClient } from './chat-data-client';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const binding = {
  capability: 'synthetic.scoped.capability', run_id: 'run-a', session_id: 'session-a', expires_at: NOW / 1000 + 300,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('chat data transport', () => {
  let fetchMock: jest.SpiedFunction<typeof fetch>;
  let workloadToken: jest.Mock<Promise<string>, []>;
  let sleep: jest.Mock<Promise<void>, [number]>;
  let client: ChatDataClient;

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    fetchMock = jest.spyOn(globalThis, 'fetch');
    workloadToken = jest.fn(async () => 'synthetic.workload.token');
    sleep = jest.fn(async (_ms: number) => undefined);
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken, sleep });
  });

  afterEach(() => jest.restoreAllMocks());

  it('exposes only the verified run and session for context tools', async () => {
    fetchMock.mockResolvedValueOnce(json(binding));
    await expect(client.sessionScope()).resolves.toEqual({ run_id: 'run-a', session_id: 'session-a' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('reads mode from the current server-issued binding without an extra exchange', async () => {
    fetchMock.mockResolvedValueOnce(json(binding));
    await expect(client.sessionScope()).resolves.toEqual({ run_id: 'run-a', session_id: 'session-a' });
    await expect(client.sessionMode()).resolves.toBe('ephemeral');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('forces a fresh workload exchange for each session lease renewal', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent', expires_at: NOW / 1000 + 280 }));
    await expect(client.renewSession()).resolves.toEqual({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' });
    await expect(client.renewSession()).resolves.toEqual({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('refuses a mode or owner change during lease renewal', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ ...binding, session_mode: 'ephemeral' }));
    await client.renewSession();
    await expect(client.renewSession()).rejects.toMatchObject({ code: 'scope_mismatch' });
  });

  it('reads server-owned session state only through its bound workload', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ mode: 'persistent', sequence: 2, health: 'active' }));
    await expect(client.sessionState()).resolves.toEqual({ mode: 'persistent', sequence: 2, health: 'active' });
    expect(fetchMock.mock.calls[1]).toEqual([
      'https://gateway.example.test/v1/chat/data/session/state', expect.objectContaining({
        body: JSON.stringify({ run_id: 'run-a', session_id: 'session-a' }),
        headers: expect.objectContaining({ Authorization: 'Bearer synthetic.scoped.capability',
          'X-Adp-Workload-Token': 'synthetic.workload.token' }),
      }),
    ]);
  });

  it('rejects server state that changes mode without ending the bound lease', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ mode: 'ephemeral', sequence: 2, health: 'active' }));
    await expect(client.sessionState()).rejects.toMatchObject({ code: 'scope_mismatch' });
  });

  it('derives scope through workload exchange and binds storage calls to the same live pod', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ status: 'empty' }));
    await expect(client.sessionRequest('draft/read', 'session-a')).resolves.toEqual({ status: 'empty' });
    expect(fetchMock.mock.calls).toEqual([
      ['https://gateway.example.test/v1/chat/data/bootstrap', expect.objectContaining({
        body: '{}', redirect: 'error', signal: expect.any(AbortSignal),
        headers: { 'Content-Type': 'application/json', 'X-Adp-Workload-Token': 'synthetic.workload.token' },
      })],
      ['https://gateway.example.test/v1/chat/data/draft/read', expect.objectContaining({
        body: JSON.stringify({ run_id: 'run-a', session_id: 'session-a' }), redirect: 'error',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer synthetic.scoped.capability', 'X-Adp-Workload-Token': 'synthetic.workload.token' },
      })],
    ]);
  });

  it('refuses data requests after the projected pod token disappears', async () => {
    workloadToken.mockResolvedValueOnce('synthetic.workload.token').mockRejectedValue(new Error('token unavailable'));
    fetchMock.mockResolvedValueOnce(json(binding));
    await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'unavailable' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });


  it.each(['run_id', 'session_id', 'user_id', 'tenant_id', 'ownerUserId', 'headers'])('rejects a supplied %s', async field => {
    await expect(client.sessionRequest('draft/write', 'session-a', { [field]: 'victim' })).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(['user_id', 'tenant_id', 'ownerUserId', 'headers'])('rejects forged activity scope %s before exchange', async field => {
    await expect(client.runRequest('activity/work', { from: '2026-10-01T00:00:00Z', to: '2026-10-04T00:00:00Z', timezone: 'UTC', [field]: 'victim' }))
      .rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('binds activity work to the exchanged chat run, not the user-supplied window', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ runs: [], issues: [], last_key: null }));
    const window = { from: '2026-10-01T00:00:00Z', to: '2026-10-04T00:00:00Z', timezone: 'UTC' };
    await expect(client.runRequest('activity/work', window)).resolves.toMatchObject({ runs: [] });
    expect(fetchMock.mock.calls[1]).toEqual([
      'https://gateway.example.test/v1/chat/data/activity/work', expect.objectContaining({
        body: JSON.stringify({ ...window, run_id: 'run-a' }),
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer synthetic.scoped.capability', 'X-Adp-Workload-Token': 'synthetic.workload.token' },
      }),
    ]);
  });

  it('shares a run capability across work and diagnostics without sharing their allowed fields', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockImplementation(async () => json({}));
    const window = { from: '2026-10-01T00:00:00Z', to: '2026-10-04T00:00:00Z', timezone: 'UTC' };
    const operations = [
      ['activity/work', window],
      ['installation/status', { installation_id: 1234 }],
      ['installation/failure', { installation_id: 1234 }],
    ] as const;
    for (const [operation, input] of operations) {
      await client.runRequest(operation, input);
      expect(fetchMock).toHaveBeenLastCalledWith(
        `https://gateway.example.test/v1/chat/data/${operation}`, expect.objectContaining({
          body: JSON.stringify({ ...input, run_id: 'run-a' }),
          headers: { 'Content-Type': 'application/json', Authorization: 'Bearer synthetic.scoped.capability', 'X-Adp-Workload-Token': 'synthetic.workload.token' },
        }),
      );
    }
    await expect(client.runRequest('activity/work', { ...window, installation_id: 1234 }))
      .rejects.toMatchObject({ code: 'invalid_request' });
    for (const operation of ['installation/status', 'installation/failure'] as const) {
      await expect(client.runRequest(operation, { installation_id: 1234, ...window }))
        .rejects.toMatchObject({ code: 'invalid_request' });
    }
    // Bind once, then authenticate the current workload on all three data requests.
    expect(workloadToken).toHaveBeenCalledTimes(4);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it('rejects a guessed session before sending a storage request', async () => {
    fetchMock.mockResolvedValueOnce(json(binding));
    await expect(client.sessionRequest('draft/read', 'session-other')).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('shares bootstrap among concurrent requests', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockImplementation(async () => json({}));
    await Promise.all([client.sessionRequest('draft/read', 'session-a'), client.sessionRequest('draft/read', 'session-a')]);
    expect(workloadToken).toHaveBeenCalledTimes(3);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('rereads the projected workload token on renewal and refuses a changed binding', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({}));
    await client.sessionRequest('draft/read', 'session-a');
    jest.spyOn(Date, 'now').mockReturnValue(NOW + 280_000);
    workloadToken.mockResolvedValue('rotated.workload.token');
    fetchMock.mockResolvedValueOnce(json({ ...binding, run_id: 'run-b', expires_at: NOW / 1000 + 580 }));
    await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(workloadToken).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[2][1]?.headers).toMatchObject({ 'X-Adp-Workload-Token': 'rotated.workload.token' });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('renews an expiring capability without changing the session', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({}));
    await client.sessionRequest('draft/read', 'session-a');
    jest.spyOn(Date, 'now').mockReturnValue(NOW + 280_000);
    fetchMock.mockResolvedValueOnce(json({ ...binding, capability: 'renewed.capability', expires_at: NOW / 1000 + 580 }))
      .mockResolvedValueOnce(json({}));
    await client.sessionRequest('draft/read', 'session-a');
    expect(fetchMock.mock.calls[3][1]?.headers).toMatchObject({ Authorization: 'Bearer renewed.capability' });
  });

  it.each([NOW / 1000, NOW / 1000 + 301])('refuses invalid capability expiry %s', async expires_at => {
    fetchMock.mockResolvedValueOnce(json({ ...binding, expires_at }));
    await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    [401, 'denied'], [403, 'denied'], [404, 'denied'], [409, 'conflict'], [410, 'expired'], [400, 'invalid_request'], [422, 'invalid_request'],
  ])('does not retry HTTP %s (%s) or turn it into empty data', async (status, code) => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ detail: 'sensitive server text' }, status));
    const failure = client.sessionRequest('draft/read', 'session-a');
    await expect(failure).rejects.toMatchObject({ status, code });
    await expect(failure).rejects.not.toThrow('sensitive');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(sleep).not.toHaveBeenCalled();
  });

  describe('error classes (DATA04)', () => {
    it.each([500, 502, 503, 504])('classifies HTTP %s as unavailable, never invalid_request, and retries once without waiting', async status => {
      fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValue(json({ detail: 'sensitive outage text' }, status));
      const failure = client.sessionRequest('draft/read', 'session-a');
      await expect(failure).rejects.toMatchObject({ code: 'unavailable', status });
      await expect(failure).rejects.not.toThrow('sensitive');
      expect(fetchMock).toHaveBeenCalledTimes(3);
      expect(sleep).not.toHaveBeenCalled();
    });

    it('classifies 429 with Retry-After seconds as rate_limited and waits that long before one identical retry', async () => {
      fetchMock.mockResolvedValueOnce(json(binding))
        .mockResolvedValueOnce(new Response('{"detail":"slow down"}', { status: 429, headers: { 'Content-Type': 'application/json', 'Retry-After': '2' } }))
        .mockResolvedValueOnce(json({ draft: {}, version: 1 }));
      await expect(client.sessionRequest('draft/write', 'session-a', { draft: {}, expected_version: 0, idempotency_key: 'write-a' }))
        .resolves.toEqual({ draft: {}, version: 1 });
      expect(sleep.mock.calls).toEqual([[2_000]]);
      expect(fetchMock.mock.calls[1][1]?.body).toBe(fetchMock.mock.calls[2][1]?.body);
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    it('classifies 429 with an HTTP-date Retry-After relative to now and caps the wait', async () => {
      const retryAt = new Date(NOW + 30_000).toUTCString();
      fetchMock.mockResolvedValueOnce(json(binding))
        .mockResolvedValue(new Response(null, { status: 429, headers: { 'Retry-After': retryAt } }));
      const failure = client.sessionRequest('draft/read', 'session-a');
      await expect(failure).rejects.toMatchObject({ code: 'rate_limited', status: 429, retryAfterMs: 30_000 });
      expect(sleep.mock.calls).toEqual([[5_000]]);
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });

    it.each([null, 'soon', '-5', '1e3'])('falls back to the default wait for Retry-After %j', async header => {
      fetchMock.mockResolvedValueOnce(json(binding))
        .mockResolvedValue(new Response(null, { status: 429, headers: header === null ? {} : { 'Retry-After': header } }));
      await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'rate_limited', retryAfterMs: 1_000 });
      expect(sleep.mock.calls).toEqual([[1_000]]);
    });

    it('clamps an absurd Retry-After instead of trusting it verbatim', async () => {
      fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValue(new Response(null, { status: 429, headers: { 'Retry-After': '999999' } }));
      await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'rate_limited', retryAfterMs: 60_000 });
      expect(sleep.mock.calls).toEqual([[5_000]]);
    });

    it('retries a rate-limited bootstrap once and never retries a denied one', async () => {
      fetchMock.mockResolvedValue(new Response(null, { status: 429, headers: { 'Retry-After': '1' } }));
      await expect(client.sessionScope()).rejects.toMatchObject({ code: 'rate_limited' });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      fetchMock.mockReset().mockResolvedValue(json({ detail: 'nope' }, 403));
      sleep.mockClear();
      await expect(client.sessionScope()).rejects.toMatchObject({ code: 'denied', status: 403 });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(sleep).not.toHaveBeenCalled();
    });

    it('keeps denied, empty and partial outcomes distinct', async () => {
      fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ detail: 'nope' }, 403))
        .mockResolvedValueOnce(json({ status: 'empty', entries: [] }))
        .mockResolvedValueOnce(json({ status: 'partial', entries: [{ id: 'art_0123456789ab' }], next_cursor: 'c1' }));
      await expect(client.sessionRequest('artifact/list', 'session-a')).rejects.toMatchObject({ code: 'denied', status: 403 });
      await expect(client.sessionRequest('artifact/list', 'session-a')).resolves.toEqual({ status: 'empty', entries: [] });
      await expect(client.sessionRequest('artifact/list', 'session-a')).resolves.toMatchObject({ status: 'partial', next_cursor: 'c1' });
      expect(fetchMock).toHaveBeenCalledTimes(4);
    });
  });

  describe('capability refresh on 401', () => {
    const write = { draft: {}, expected_version: 0, idempotency_key: 'write-a' };
    const renewed = { ...binding, capability: 'renewed.capability' };

    it.each(['capability_expired', 'capability_invalid'])('refreshes once on 401 %s and retries the identical request with the new capability', async error => {
      fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ error }, 401))
        .mockResolvedValueOnce(json(renewed)).mockResolvedValueOnce(json({ draft: {}, version: 1 }));
      await expect(client.sessionRequest('draft/write', 'session-a', write)).resolves.toEqual({ draft: {}, version: 1 });
      expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
        'https://gateway.example.test/v1/chat/data/bootstrap', 'https://gateway.example.test/v1/chat/data/draft/write',
        'https://gateway.example.test/v1/chat/data/bootstrap', 'https://gateway.example.test/v1/chat/data/draft/write',
      ]);
      expect(fetchMock.mock.calls[1][1]?.body).toBe(fetchMock.mock.calls[3][1]?.body);
      expect(fetchMock.mock.calls[1][1]?.headers).toMatchObject({ Authorization: 'Bearer synthetic.scoped.capability' });
      expect(fetchMock.mock.calls[3][1]?.headers).toMatchObject({ Authorization: 'Bearer renewed.capability' });
      expect(workloadToken).toHaveBeenCalledTimes(4);
      expect(sleep).not.toHaveBeenCalled();
    });

    it.each(['capability_expired', 'capability_invalid'])('recognizes the actual FastAPI 401 detail envelope for %s', async error => {
      fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ detail: { error } }, 401))
        .mockResolvedValueOnce(json(renewed)).mockResolvedValueOnce(json({ status: 'ok' }));
      await expect(client.sessionRequest('draft/read', 'session-a')).resolves.toEqual({ status: 'ok' });
      expect(fetchMock).toHaveBeenCalledTimes(4);
    });

    it('surfaces denied after exactly one refresh when the retry is refused again', async () => {
      fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ error: 'capability_expired' }, 401))
        .mockResolvedValueOnce(json(renewed)).mockResolvedValueOnce(json({ error: 'capability_invalid' }, 401));
      await expect(client.sessionRequest('draft/write', 'session-a', write)).rejects.toMatchObject({ code: 'denied', status: 401 });
      expect(fetchMock).toHaveBeenCalledTimes(4);
    });

    it.each([
      () => json({ error: 'scope_refused' }, 401),
      () => json({ error: 'capability_expired', message: 'sensitive' }, 401),
      () => json({ detail: { error: 'capability_expired', message: 'sensitive' } }, 401),
      () => new Response('{"error":"capability_expired"}', { status: 401, headers: { 'Content-Type': 'text/plain' } }),
      () => new Response(null, { status: 401 }),
    ])('keeps any other 401 as denied without refreshing', async response => {
      fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(response());
      const failure = client.sessionRequest('draft/read', 'session-a');
      await expect(failure).rejects.toMatchObject({ code: 'denied', status: 401 });
      await expect(failure).rejects.not.toThrow('sensitive');
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(workloadToken).toHaveBeenCalledTimes(2);
    });

    it.each(['capability_expired', 'capability_invalid'])('treats a bootstrap 401 %s as denied immediately without looping', async error => {
      fetchMock.mockResolvedValue(json({ error }, 401));
      await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'denied', status: 401 });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(workloadToken).toHaveBeenCalledTimes(1);
    });

    it('still refuses a changed binding on refresh', async () => {
      fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json({ error: 'capability_expired' }, 401))
        .mockResolvedValueOnce(json({ ...renewed, run_id: 'run-b' }));
      await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'scope_mismatch' });
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });
  });

  it('retries a lost write response with exactly the same body and idempotency key', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockRejectedValueOnce(new Error('sensitive fetch details')).mockResolvedValueOnce(json({}));
    await client.sessionRequest('draft/write', 'session-a', { draft: {}, expected_version: 0, idempotency_key: 'write-a' });
    expect(fetchMock.mock.calls[1][1]?.body).toBe(fetchMock.mock.calls[2][1]?.body);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each([
    [404, { detail: { error: 'chat_artifact_missing' } }, 'missing'],
    [404, { detail: { error: 'chat_scope_refused' } }, 'denied'],
    [404, { detail: { error: 'unknown_error' } }, 'denied'],
    [404, { error: 'chat_artifact_missing' }, 'denied'],
    [404, { detail: [{ error: 'chat_artifact_missing' }] }, 'denied'],
    [404, { detail: { error: 'chat_artifact_missing', message: 'sensitive' } }, 'denied'],
    [404, { detail: { error: 'chat_artifact_missing' }, message: 'sensitive' }, 'denied'],
    [401, { detail: { error: 'chat_artifact_missing' } }, 'denied'],
    [403, { detail: { error: 'chat_artifact_missing' } }, 'denied'],
  ])('classifies HTTP %s with envelope %j as %s without retrying', async (status, envelope, code) => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(json(envelope, status as number));
    const failure = client.downloadArtifact('session-a', 'art_0123456789ab');
    await expect(failure).rejects.toMatchObject({ code, status });
    await expect(failure).rejects.not.toThrow('sensitive');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    () => new Response('{"detail":', { status: 404, headers: { 'Content-Type': 'application/json' } }),
    () => new Response('{"detail":{"error":"chat_artifact_missing"}}', { status: 404, headers: { 'Content-Type': 'text/html' } }),
    () => new Response(null, { status: 404, headers: { 'Content-Type': 'application/json' } }),
    () => new Response(' '.repeat(4096) + '{"detail":{"error":"chat_artifact_missing"}}', { status: 404, headers: { 'Content-Type': 'application/json' } }),
    () => new Response(new ReadableStream({ start(controller) { controller.error(new Error('sensitive')); } }), { status: 404, headers: { 'Content-Type': 'application/json' } }),
  ])('keeps malformed or unreadable error responses non-enumerating', async response => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(response());
    const failure = client.downloadArtifact('session-a', 'art_0123456789ab');
    await expect(failure).rejects.toMatchObject({ code: 'denied', status: 404 });
    await expect(failure).rejects.not.toThrow('sensitive');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('bounds chunked error bodies and cancels the remaining stream', async () => {
    const cancel = jest.fn();
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(Buffer.alloc(2048, ' '));
        controller.enqueue(Buffer.alloc(2049, ' '));
        controller.enqueue(Buffer.from('{"detail":{"error":"chat_artifact_missing"}}'));
      },
      cancel,
    });
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(new Response(stream, {
      status: 404, headers: { 'Content-Type': 'application/json', 'Content-Length': '1' },
    }));
    await expect(client.downloadArtifact('session-a', 'art_0123456789ab')).rejects.toMatchObject({ code: 'denied', status: 404 });
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('bounds outages and does not leak transport details', async () => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockRejectedValue(new Error('sensitive capability and URL'));
    const failure = client.sessionRequest('draft/read', 'session-a');
    await expect(failure).rejects.toMatchObject({ code: 'unavailable' });
    await expect(failure).rejects.not.toThrow('sensitive');
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('aborts a hung request and bounds retries', async () => {
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken, timeoutMs: 5 });
    fetchMock.mockImplementation((_url, options) => new Promise((_resolve, reject) => {
      options?.signal?.addEventListener('abort', () => reject(new Error('timeout')), { once: true });
    }));
    await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'unavailable' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    () => new Response('not JSON', { headers: { 'Content-Type': 'application/json' } }),
    () => new Response('{}', { headers: { 'Content-Type': 'text/html' } }),
    () => json('oversized'.repeat(300_000)),
  ])('rejects malformed or oversized responses', async response => {
    fetchMock.mockResolvedValueOnce(json(binding)).mockResolvedValueOnce(response());
    await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each(['http://gateway.example.test', 'https://secret@gateway.example.test', 'https://gateway.example.test/path', 'https://gateway.example.test?token=secret'])('rejects unsafe base URLs', baseUrl => {
    expect(() => new ChatDataClient({ baseUrl, workloadToken })).toThrow('Chat data invalid_request');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('never makes an anonymous request when workload credentials are unavailable', async () => {
    workloadToken.mockRejectedValue(new Error('sensitive file path'));
    await expect(client.sessionRequest('draft/read', 'session-a')).rejects.toMatchObject({ code: 'unavailable' });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
