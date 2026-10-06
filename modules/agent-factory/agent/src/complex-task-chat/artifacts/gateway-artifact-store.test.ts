import { createHash } from 'crypto';
import { mkdir, mkdtemp, readFile, rename, rm, symlink, writeFile } from 'fs/promises';
import files from 'fs/promises';
import os from 'os';
import path from 'path';
import { ChatDataClient } from '../gateway/chat-data-client';
import { GatewayArtifactStore } from './gateway-artifact-store';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const ARTIFACT_ID = 'art_0123456789abcdef';
const SECOND_ID = 'art_abcdef0123456789';
const content = Buffer.from('A durable result\n');
const checksum = createHash('sha256').update(content).digest('hex');
const reference = {
  id: ARTIFACT_ID, filename: 'result.txt', contentType: 'text/plain', sizeBytes: content.length, checksum,
  createdAt: new Date(NOW).toISOString(), source: 'agent', scanStatus: 'not_scanned',
  url: `/v1/chat/data/artifact/session-a/${ARTIFACT_ID}?run_id=run-a`,
  urlExpiresAt: new Date(NOW + 86_400_000).toISOString(),
};
const bootstrap = { capability: 'synthetic.capability', run_id: 'run-a', session_id: 'session-a', expires_at: NOW / 1000 + 300 };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function page(entries: unknown[] = [], cursor: string | null = null, missing: string[] = []) {
  return {
    status: cursor || missing.length ? 'partial' : entries.length ? 'ok' : 'empty', entries,
    next_cursor: cursor, observed_at: new Date(NOW).toISOString(),
    coverage: { source: 'session_artifacts', complete: !cursor && !missing.length, missing_source_ids: missing },
  };
}

function download(body: Buffer = content, headers: Record<string, string> = {}): Response {
  return new Response(new Uint8Array(body), { headers: { 'Content-Type': 'text/plain', 'X-Artifact-Scan-Status': 'clean', ...headers } });
}

describe('gateway artifact port through the real transport', () => {
  let workspace: string;
  let outside: string;
  let fetchMock: jest.SpiedFunction<typeof fetch>;
  let store: GatewayArtifactStore;

  beforeEach(async () => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    workspace = await mkdtemp(path.join(os.tmpdir(), 'adp-artifact-workspace-'));
    outside = await mkdtemp(path.join(os.tmpdir(), 'adp-artifact-outside-'));
    await writeFile(path.join(workspace, 'result.txt'), content);
    fetchMock = jest.spyOn(globalThis, 'fetch').mockResolvedValueOnce(json(bootstrap));
    store = new GatewayArtifactStore(new ChatDataClient({
      baseUrl: 'https://gateway.example.test', workloadToken: async () => 'synthetic.workload.token',
    }), 'session-a', workspace);
  });

  afterEach(async () => {
    jest.restoreAllMocks();
    await rm(workspace, { recursive: true, force: true });
    await rm(outside, { recursive: true, force: true });
  });

  it('publishes bounded bytes with server-derived ownership and stable retry identity', async () => {
    fetchMock.mockResolvedValueOnce(json(reference)).mockResolvedValueOnce(json(reference));
    const input = { sessionId: 'session-a', taskId: 'turn-a', localPath: 'result.txt', identity: { userId: 'victim', orgId: 'other' } };
    const result = await store.publish(input);
    await store.publish(input);
    expect(result).toEqual({ ...reference, scanStatus: undefined, url: `https://gateway.example.test${reference.url}` });
    expect(fetchMock.mock.calls[1][1]?.body).toBe(fetchMock.mock.calls[2][1]?.body);
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({
      filename: 'result.txt', content_type: 'text/plain', content_sha256: checksum,
      content_base64: content.toString('base64'), idempotency_key: expect.stringMatching(/^[a-f0-9]{64}$/),
      run_id: 'run-a', session_id: 'session-a',
    });
  });

  it('retries a lost upload response without changing the request', async () => {
    fetchMock.mockRejectedValueOnce(new Error('lost response')).mockResolvedValueOnce(json(reference));
    await store.publish({ sessionId: 'session-a', localPath: 'result.txt' });
    expect(fetchMock.mock.calls[1][1]?.body).toBe(fetchMock.mock.calls[2][1]?.body);
  });

  it('retains replacement lineage without allowing model-selected source or retention', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...reference, supersedes: SECOND_ID }));
    const result = await store.publish({ sessionId: 'session-a', localPath: 'result.txt', supersedes: SECOND_ID });
    expect(result.supersedes).toBe(SECOND_ID);
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string).supersedes).toBe(SECOND_ID);
    await expect(store.publish({ sessionId: 'session-a', localPath: 'result.txt', source: 'user' })).rejects.toMatchObject({ code: 'invalid_request' });
    await expect(store.publish({ sessionId: 'session-a', localPath: 'result.txt', ttl: 1 })).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    { checksum: '0'.repeat(64) }, { sizeBytes: 1 }, { filename: 'different.txt' },
    { contentType: 'application/json' }, { source: 'user' }, { scanStatus: 'infected' },
  ])('rejects an inconsistent publication receipt', async changes => {
    fetchMock.mockResolvedValueOnce(json({ ...reference, ...changes }));
    await expect(store.publish({ sessionId: 'session-a', localPath: 'result.txt' })).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('returns gateway URLs even if an upstream reference contains a direct object URL', async () => {
    fetchMock.mockResolvedValueOnce(json(page([{ ...reference, url: 'https://objects.example.test/private?signature=secret' }])));
    const results = await store.listBySession('session-a');
    expect(results[0].url).toBe(`https://gateway.example.test${reference.url}`);
    expect(JSON.stringify(results)).not.toContain('signature');
  });

  it('drains empty filtered pages and preserves filters and page size while following cursors', async () => {
    fetchMock.mockResolvedValueOnce(json(page([], 'cursor-a')))
      .mockResolvedValueOnce(json(page([reference], 'cursor-b')))
      .mockResolvedValueOnce(json(page([{ ...reference, id: SECOND_ID }])));
    const results = await store.listBySession('session-a', { limit: 2, contentType: 'text/plain', filename: 'result' });
    expect(results.map(result => result.id)).toEqual([ARTIFACT_ID, SECOND_ID]);
    const bodies = fetchMock.mock.calls.slice(1).map(([, options]) => JSON.parse(options?.body as string));
    expect(bodies).toEqual([undefined, 'cursor-a', 'cursor-b'].map(cursor => ({
      run_id: 'run-a', session_id: 'session-a', limit: 2, content_type: 'text/plain', filename: 'result',
      ...(cursor ? { cursor } : {}),
    })));
  });

  it('returns empty only for a complete empty listing', async () => {
    fetchMock.mockResolvedValueOnce(json(page()));
    await expect(store.listBySession('session-a')).resolves.toEqual([]);
  });

  it('does not hide missing sources even when enough other results satisfy the limit', async () => {
    fetchMock.mockResolvedValueOnce(json(page([reference], null, [SECOND_ID])));
    await expect(store.listBySession('session-a', { limit: 1 })).rejects.toMatchObject({ code: 'incomplete' });
  });

  it('detects looping pagination rather than returning a false empty result', async () => {
    fetchMock.mockImplementation(async () => json(page([], 'repeated')));
    await expect(store.listBySession('session-a')).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each([
    { ...page(), status: 'partial' },
    { ...page(), coverage: { source: 'session_artifacts', complete: false, missing_source_ids: [] } },
    page([reference, reference]),
  ])('rejects inconsistent listing coverage or duplicate records', async result => {
    fetchMock.mockResolvedValueOnce(json(result));
    await expect(store.listBySession('session-a')).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('downloads only through the scoped gateway and atomically replaces the destination', async () => {
    fetchMock.mockResolvedValueOnce(download());
    await writeFile(path.join(workspace, 'download.txt'), 'old content');
    await store.fetch(ARTIFACT_ID, 'download.txt', 'session-a', { userId: 'victim' });
    expect(await readFile(path.join(workspace, 'download.txt'))).toEqual(content);
    expect(fetchMock.mock.calls[1]).toEqual([
      `https://gateway.example.test${reference.url}`,
      expect.objectContaining({ method: 'GET', body: undefined, redirect: 'error', headers: { Authorization: 'Bearer synthetic.capability' } }),
    ]);
  });

  it.each([false, true])('creates missing nested destination directories (absolute path: %s)', async absolute => {
    fetchMock.mockResolvedValueOnce(download());
    const destination = absolute ? path.join(workspace, 'new/sub/result.txt') : 'new/sub/result.txt';
    await store.fetch(ARTIFACT_ID, destination, 'session-a');
    expect(await readFile(path.join(workspace, 'new/sub/result.txt'))).toEqual(content);
  });

  it('creates nested directories through an existing symlink confined to the workspace', async () => {
    const parent = path.join(workspace, 'existing');
    await mkdir(parent);
    await symlink(parent, path.join(workspace, 'alias'));
    fetchMock.mockResolvedValueOnce(download());
    await store.fetch(ARTIFACT_ID, 'alias/new/sub/result.txt', 'session-a');
    expect(await readFile(path.join(parent, 'new/sub/result.txt'))).toEqual(content);
  });

  it('handles concurrent downloads creating the same parent directories', async () => {
    fetchMock.mockImplementation(async () => download());
    await Promise.all([
      store.fetch(ARTIFACT_ID, 'new/sub/first.txt', 'session-a'),
      store.fetch(SECOND_ID, 'new/sub/second.txt', 'session-a'),
    ]);
    expect(await readFile(path.join(workspace, 'new/sub/first.txt'))).toEqual(content);
    expect(await readFile(path.join(workspace, 'new/sub/second.txt'))).toEqual(content);
  });

  it('does not create destination directories when the gateway denies the download', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'refused' }, 403));
    await expect(store.fetch(ARTIFACT_ID, 'new/sub/result.txt', 'session-a')).rejects.toMatchObject({ status: 403 });
    await expect(files.stat(path.join(workspace, 'new'))).rejects.toMatchObject({ code: 'ENOENT' });
  });

  it('rejects missing destination parents outside the workspace or behind an escaping symlink', async () => {
    await symlink(outside, path.join(workspace, 'outside-link'));
    const destinations = [
      'outside-link/new/sub/result.txt',
      path.join(outside, 'new/sub/result.txt'),
      path.relative(workspace, path.join(outside, 'new/sub/result.txt')),
    ];
    for (const destination of destinations) {
      await expect(store.fetch(ARTIFACT_ID, destination, 'session-a')).rejects.toMatchObject({ code: 'invalid_request' });
    }
    await expect(files.stat(path.join(outside, 'new'))).rejects.toMatchObject({ code: 'ENOENT' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not follow a symlink replacing a newly created directory before it is opened', async () => {
    fetchMock.mockResolvedValueOnce(download());
    const actualMkdir = files.mkdir;
    jest.spyOn(files, 'mkdir').mockImplementation(async (filename, options) => {
      const result = await actualMkdir(filename, options);
      if (path.basename(filename.toString()) === 'new') {
        await rename(path.join(workspace, 'new'), path.join(workspace, 'new-original'));
        await symlink(outside, path.join(workspace, 'new'));
      }
      return result;
    });
    await expect(store.fetch(ARTIFACT_ID, 'new/sub/result.txt', 'session-a')).rejects.toMatchObject({
      code: expect.stringMatching(/^(ENOTDIR|ELOOP)$/),
    });
    await expect(files.stat(path.join(outside, 'sub'))).rejects.toMatchObject({ code: 'ENOENT' });
    await expect(files.stat(path.join(workspace, 'new-original/sub'))).rejects.toMatchObject({ code: 'ENOENT' });
  });

  it('keeps directory creation anchored when an opened parent is replaced during download', async () => {
    const parent = path.join(workspace, 'existing');
    await mkdir(parent);
    fetchMock.mockImplementationOnce(async () => {
      await rename(parent, `${parent}-original`);
      await symlink(outside, parent);
      return download();
    });
    await store.fetch(ARTIFACT_ID, 'existing/new/sub/result.txt', 'session-a');
    expect(await readFile(path.join(`${parent}-original`, 'new/sub/result.txt'))).toEqual(content);
    await expect(files.stat(path.join(outside, 'new'))).rejects.toMatchObject({ code: 'ENOENT' });
  });

  it.each([401, 403, 404, 409, 410])('preserves the existing destination when download returns HTTP %s', async status => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'refused' }, status));
    await writeFile(path.join(workspace, 'download.txt'), 'original');
    await expect(store.fetch(ARTIFACT_ID, 'download.txt', 'session-a')).rejects.toMatchObject({ status });
    expect(await readFile(path.join(workspace, 'download.txt'), 'utf8')).toBe('original');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    () => download(content, { 'Content-Length': '9999' }),
    () => download(Buffer.alloc(0)),
    () => download(content, { 'X-Artifact-Scan-Status': 'infected' }),
    () => download(Buffer.alloc(8 * 1024 * 1024 + 1)),
  ])('refuses incomplete, quarantined or oversized download bodies', async response => {
    fetchMock.mockResolvedValueOnce(response());
    await expect(store.fetch(ARTIFACT_ID, 'download.txt', 'session-a')).rejects.toMatchObject({ code: 'invalid_response' });
    await expect(readFile(path.join(workspace, 'download.txt'))).rejects.toMatchObject({ code: 'ENOENT' });
  });

  it('never interprets an object URL or path as an artifact ID', async () => {
    for (const id of ['https://objects.example.test/file', '../secret', 'o/tenant/u/user/file']) {
      await expect(store.fetch(id, 'download.txt', 'session-a')).rejects.toMatchObject({ code: 'invalid_request' });
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses cross-session access without reading files or making requests', async () => {
    await expect(store.publish({ sessionId: 'other', localPath: 'absent' })).rejects.toMatchObject({ code: 'scope_mismatch' });
    await expect(store.fetch(ARTIFACT_ID, 'download.txt', 'other')).rejects.toMatchObject({ code: 'scope_mismatch' });
    await expect(store.listBySession('other')).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses uploads and destination parents escaping the workspace through symlinks', async () => {
    await writeFile(path.join(outside, 'secret'), 'not an artifact');
    await symlink(path.join(outside, 'secret'), path.join(workspace, 'secret-link'));
    await symlink(outside, path.join(workspace, 'outside-link'));
    for (const localPath of [path.join(outside, 'secret'), 'secret-link']) {
      await expect(store.publish({ sessionId: 'session-a', localPath })).rejects.toMatchObject({ code: 'invalid_request' });
    }
    await expect(store.fetch(ARTIFACT_ID, 'outside-link/secret', 'session-a')).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(await readFile(path.join(outside, 'secret'), 'utf8')).toBe('not an artifact');
  });

  it('replaces a destination symlink without overwriting its external target', async () => {
    await writeFile(path.join(outside, 'secret'), 'untouched');
    await symlink(path.join(outside, 'secret'), path.join(workspace, 'download.txt'));
    fetchMock.mockResolvedValueOnce(download());
    await store.fetch(ARTIFACT_ID, 'download.txt', 'session-a');
    expect(await readFile(path.join(outside, 'secret'), 'utf8')).toBe('untouched');
    expect(await readFile(path.join(workspace, 'download.txt'))).toEqual(content);
  });

  it('refuses an upload whose parent becomes an external symlink between path checking and open', async () => {
    const parent = path.join(workspace, 'nested');
    await mkdir(parent);
    await writeFile(path.join(parent, 'result.txt'), content);
    await writeFile(path.join(outside, 'result.txt'), 'private');
    const actualOpen = files.open;
    jest.spyOn(files, 'open').mockImplementation(async (filename, flags, mode) => {
      if (filename === path.join(parent, 'result.txt')) {
        await rename(parent, `${parent}-original`);
        await symlink(outside, parent);
      }
      return actualOpen(filename, flags, mode);
    });
    await expect(store.publish({ sessionId: 'session-a', localPath: 'nested/result.txt' })).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([0, 8 * 1024 * 1024 + 1])('refuses upload size %s before calling the gateway', async size => {
    await writeFile(path.join(workspace, 'invalid.bin'), Buffer.alloc(size));
    await expect(store.publish({ sessionId: 'session-a', localPath: 'invalid.bin' })).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('binds artifact tools to the trusted session and calls onPublish only after success', async () => {
    fetchMock.mockResolvedValueOnce(json(reference));
    const onPublish = jest.fn();
    const tools = store.toolsForTurn({ sessionId: 'session-a', taskId: 'turn-a', onPublish });
    await tools.find(tool => tool.name === 'publish_artifact')!.handler({
      path: 'result.txt', sessionId: 'victim', identity: { userId: 'victim' }, source: 'user', ttl: 1,
    });
    expect(onPublish).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toMatchObject({ session_id: 'session-a', run_id: 'run-a' });
    expect(tools.every(tool => !Object.keys(tool.inputSchema).includes('sessionId'))).toBe(true);
  });
});
