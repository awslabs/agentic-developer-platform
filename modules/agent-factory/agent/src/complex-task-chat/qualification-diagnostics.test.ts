import { EventEmitter } from 'node:events';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { open, readdir, unlink } from 'node:fs/promises';
import { connect } from 'node:net';
import { QualificationBinding, qualificationBinding, qualificationDigest, qualificationManifest } from './qualification-contract';
import { QualificationDiagnostics } from './qualification-diagnostics';

jest.mock('node:fs/promises');
jest.mock('node:child_process');
jest.mock('node:net');

const binding = { evaluation_id: 'evaluation', actor: 'a1' } as QualificationBinding;
const signal = () => new AbortController().signal;
const digest = (value: string) => createHash('sha256').update(value).digest('hex');

function processDouble() {
  const child = Object.assign(new EventEmitter(), {
    pid: 4567, exitCode: null, signalCode: null, stdout: new EventEmitter(), stderr: new EventEmitter(),
    unref: jest.fn(), kill: jest.fn(),
  });
  child.kill.mockImplementation(() => { child.emit('exit', null, 'SIGKILL'); return true; });
  jest.mocked(spawn).mockReturnValue(child as unknown as ReturnType<typeof spawn>);
  return child;
}

describe('bounded diagnostics in the admitted sandbox', () => {
  beforeEach(() => {
    jest.replaceProperty(process, 'env', {});
    jest.mocked(readdir).mockResolvedValue([]);
    jest.mocked(open).mockRejectedValue(Object.assign(new Error('absent'), { code: 'ENOENT' }));
    jest.mocked(unlink).mockResolvedValue();
  });
  afterEach(() => { jest.restoreAllMocks(); jest.resetAllMocks(); });

  it('returns only exposure digests, never environment credentials', async () => {
    const diagnostics = new QualificationDiagnostics(binding);
    expect(await diagnostics.execute('environment', signal())).toMatchObject({ outcome: 'absent', exposure_digests: [] });
    process.env.AWS_SECRET_ACCESS_KEY = 'synthetic-secret';
    process.env.ANTHROPIC_API_KEY = 'synthetic-model-key';
    process.env.NORMAL_SETTING = 'not-a-secret';
    const result = await diagnostics.execute('environment', signal());
    expect(result).toEqual({ probe: 'environment', executed: true, outcome: 'present',
      exposure_digests: [digest('AWS_SECRET_ACCESS_KEY=synthetic-secret'), digest('ANTHROPIC_API_KEY=synthetic-model-key')] });
    expect(JSON.stringify(result)).not.toContain('synthetic');
  });

  it('reads bounded proc contents and closes every handle without returning values', async () => {
    jest.mocked(readdir).mockResolvedValue(['123'] as never);
    const content = Buffer.from('NORMAL_SETTING=value\0GITHUB_TOKEN=synthetic-token\0');
    const close = jest.fn(async () => undefined);
    jest.mocked(open).mockResolvedValue({ read: jest.fn(async (buffer: Buffer) => {
      content.copy(buffer); return { bytesRead: content.length, buffer };
    }), close } as unknown as Awaited<ReturnType<typeof open>>);
    const result = await new QualificationDiagnostics(binding).execute('proc', signal());
    expect(result).toMatchObject({ outcome: 'present', exposure_digests: [digest('GITHUB_TOKEN=synthetic-token')] });
    expect(close).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(result)).not.toContain('synthetic-token');
  });

  it('rejects oversized process inventories rather than returning absence', async () => {
    jest.mocked(readdir).mockResolvedValue(Array.from({ length: 129 }, (_, index) => String(index)) as never);
    await expect(new QualificationDiagnostics(binding).execute('proc', signal())).rejects.toThrow('bound exceeded');
  });

  it('does not fabricate observations for unsupported operations', async () => {
    await expect(new QualificationDiagnostics(binding).execute('memory_write', signal())).resolves.toBeUndefined();
    expect(spawn).not.toHaveBeenCalled();
  });

  it('creates and cleans only its run-owned file and fixed background process', async () => {
    const child = processDouble();
    const writeFile = jest.fn(async () => undefined);
    const close = jest.fn(async () => undefined);
    jest.mocked(open).mockResolvedValue({ writeFile, close } as unknown as Awaited<ReturnType<typeof open>>);
    const diagnostics = new QualificationDiagnostics(binding);
    const executing = diagnostics.execute('sandbox_identity', signal());
    await new Promise(resolve => setImmediate(resolve));
    child.emit('spawn');
    expect(await executing).toMatchObject({ outcome: 'distinct' });
    expect(writeFile).toHaveBeenCalledWith(`adp-e45-residue=${digest(binding.evaluation_id)}`);
    await diagnostics.cleanup();
    expect(child.kill).toHaveBeenCalledWith('SIGKILL');
    expect(unlink).toHaveBeenCalledWith(`/tmp/adp-e45-${digest(binding.evaluation_id)}`);
    expect(close).toHaveBeenCalledTimes(1);
  });

  it.each(['a2', 'b1'] as const)('detects A1 residue in %s without creating or deleting it', async actor => {
    const content = Buffer.from(`adp-e45-residue=${digest(binding.evaluation_id)}`);
    jest.mocked(open).mockResolvedValue({ close: jest.fn(async () => undefined),
      read: jest.fn(async (buffer: Buffer) => { content.copy(buffer); return { bytesRead: content.length, buffer }; }),
    } as unknown as Awaited<ReturnType<typeof open>>);
    jest.mocked(readdir).mockResolvedValue(['123'] as never);
    const diagnostics = new QualificationDiagnostics({ ...binding, actor });
    expect(await diagnostics.execute('residue_file', signal())).toMatchObject({ outcome: 'present' });
    expect(await diagnostics.execute('residue_process', signal())).toMatchObject({ outcome: 'present' });
    await diagnostics.cleanup();
    expect(unlink).not.toHaveBeenCalled();
    expect(spawn).not.toHaveBeenCalled();
  });

  it('still removes its file when process cleanup fails and reports that failure', async () => {
    const child = processDouble();
    jest.mocked(open).mockResolvedValue({ writeFile: jest.fn(async () => undefined), close: jest.fn(async () => undefined)
    } as unknown as Awaited<ReturnType<typeof open>>);
    const diagnostics = new QualificationDiagnostics(binding);
    const executing = diagnostics.execute('sandbox_identity', signal());
    await new Promise(resolve => setImmediate(resolve));
    child.emit('spawn');
    await executing;
    child.kill.mockImplementation(() => { throw new Error('synthetic cleanup failure'); });
    await expect(diagnostics.cleanup()).rejects.toThrow('process cleanup unverified');
    expect(unlink).toHaveBeenCalledWith(`/tmp/adp-e45-${digest(binding.evaluation_id)}`);
  });

  it('bounds a stuck AWS provider by killing the diagnostic process group on cancellation', async () => {
    const child = processDouble();
    const kill = jest.spyOn(process, 'kill').mockImplementation(() => { child.emit('close', null); return true; });
    const cancellation = new AbortController();
    const executing = new QualificationDiagnostics(binding).execute('aws_provider', cancellation.signal);
    cancellation.abort();
    await expect(executing).rejects.toThrow('cancelled');
    expect(kill).toHaveBeenCalledWith(-child.pid, 'SIGKILL');
    expect(spawn).toHaveBeenCalledWith(process.execPath, ['-e', expect.any(String), 'aws_provider', '{}', expect.any(String)],
      { detached: true, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
  });

  it('redacts successful AWS CLI output and bounds oversized output', async () => {
    const child = processDouble();
    jest.spyOn(process, 'kill').mockReturnValue(true);
    const executing = new QualificationDiagnostics(binding).execute('aws_cli', signal());
    child.stdout.emit('data', Buffer.from('synthetic-identity'));
    child.emit('close', 0);
    expect(await executing).toMatchObject({ outcome: 'allowed', exposure_digests: [digest('synthetic-identity')] });
    const oversized = new QualificationDiagnostics(binding).execute('aws_cli', signal());
    child.stdout.emit('data', Buffer.alloc(65537));
    child.emit('close', null);
    await expect(oversized).rejects.toThrow('output exceeded');
  });

  it('resolves the packaged SDK outside the application working directory without network or inherited credentials', async () => {
    const child = processDouble();
    jest.spyOn(process, 'kill').mockReturnValue(true);
    const executing = new QualificationDiagnostics(binding).execute('aws_provider', signal());
    const args = [...jest.mocked(spawn).mock.calls[0][1] as string[]];
    child.stdout.emit('data', Buffer.from('{"outcome":"denied","exposure_digests":[]}'));
    child.emit('close', 0);
    await executing;
    const filesystem = jest.requireActual<typeof import('node:fs')>('node:fs');
    const { tmpdir } = jest.requireActual<typeof import('node:os')>('node:os');
    const { join } = jest.requireActual<typeof import('node:path')>('node:path');
    const { execFileSync } = jest.requireActual<typeof import('node:child_process')>('node:child_process');
    const directory = filesystem.mkdtempSync(join(tmpdir(), 'e45-sdk-'));
    try {
      args[1] = "require('node:net').Socket.prototype.connect = () => { throw new Error('offline network forbidden'); };\n" + args[1];
      const output = execFileSync(process.execPath, args, { cwd: directory, timeout: 5000, encoding: 'utf8',
        env: { HOME: directory, AWS_EC2_METADATA_DISABLED: 'true', AWS_SHARED_CREDENTIALS_FILE: join(directory, 'missing'),
          AWS_CONFIG_FILE: join(directory, 'missing') } });
      expect(JSON.parse(output)).toEqual({ outcome: 'denied', exposure_digests: [] });
    } finally { filesystem.rmSync(directory, { recursive: true, force: true }); }
  });

  it.each(['s3_read', 'dynamodb_read', 'secrets_read'])('runs %s with fixed resource references and redacted observations', async probe => {
    const child = processDouble();
    jest.spyOn(process, 'kill').mockReturnValue(true);
    const owner = qualificationBinding.parse({ ...binding, nonce: 'a'.repeat(64), manifest_digest: qualificationDigest, manifest_version: 'e45-v1',
      target: 'test-target', tenant_id: 'tenant', user_id: 'owner', team_id: '', session_id: 'session-a', run_id: 'run-a',
      sandbox_uid: 'pod-a', lease_generation: 1, attempt: 1, credential_epoch: 1, image_digest: `sha256:${'b'.repeat(64)}`,
      issued_at: 1000, expires_at: 1120, limits: qualificationManifest.limits });
    const fixtures = { owner, message_id: 'msg-a', summary_id: 'sum-a', artifact_id: `art_${'a'.repeat(32)}`,
      memory_id: `mem_${'b'.repeat(32)}`, cursor: 'opaque', history_version: 3, memory_version: 1, cursor_expires_at: 1120,
      bucket: 'synthetic-bucket', object_key: 'synthetic-key', context_table: 'synthetic-table', secret_canary: 'synthetic-canary', region: 'us-east-1' };
    const executing = new QualificationDiagnostics(owner).execute(probe, signal(), fixtures);
    const [executable, args, options] = jest.mocked(spawn).mock.calls[0];
    expect(executable).toBe(process.execPath);
    expect(args?.[0]).toBe('-e');
    expect(args?.[2]).toBe(probe);
    expect(JSON.parse(String(args?.[3]))).toEqual({ bucket: fixtures.bucket, object_key: fixtures.object_key,
      context_table: fixtures.context_table, session_id: owner.session_id, secret_canary: fixtures.secret_canary, region: fixtures.region });
    expect(options).toEqual({ detached: true, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
    child.stdout.emit('data', Buffer.from(JSON.stringify({ outcome: 'allowed', exposure_digests: [digest('synthetic-secret')] })));
    child.emit('close', 0);
    const result = await executing;
    expect(result).toMatchObject({ probe, executed: true, outcome: 'allowed', exposure_digests: [digest('synthetic-secret')] });
    expect(JSON.stringify(result)).not.toContain('synthetic-secret');
    await expect(new QualificationDiagnostics(owner).execute(probe, signal(), { ...fixtures, secret_canary: 'canary; arbitrary-command' })).rejects.toThrow();
  });

  it('distinguishes blocked TCP timeouts from cancellation and DNS failure', async () => {
    const socket = Object.assign(new EventEmitter(), { destroy: jest.fn(), setTimeout: jest.fn() });
    jest.mocked(connect).mockReturnValue(socket as unknown as ReturnType<typeof connect>);
    const diagnostics = new QualificationDiagnostics(binding);
    const dropped = diagnostics.execute('metadata', signal());
    socket.setTimeout.mock.calls[0][1]();
    expect(await dropped).toMatchObject({ outcome: 'denied' });
    const failed = diagnostics.execute('network_routes', signal());
    socket.emit('error', Object.assign(new Error(), { code: 'ENOTFOUND' }));
    await expect(failed).rejects.toThrow('unavailable');
    const cancellation = new AbortController();
    const cancelled = diagnostics.execute('kubernetes_api', cancellation.signal);
    cancellation.abort();
    socket.emit('error', Object.assign(new Error(), { code: 'ABORT_ERR' }));
    await expect(cancelled).rejects.toThrow();
  });
});
