import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MAX_ARTIFACT_BYTES, uploadRunArtifact } from './artifactGateway';
import { saveToS3Fallback } from '../utils/ghPost';
import { S3Fallback } from '../services/S3Fallback';

jest.mock('./runIdentity', () => ({
  ...jest.requireActual('./runIdentity'),
  workerAwsCredentials: () => async () => ({ accessKeyId: 'PLATFORMTESTKEY', secretAccessKey: 'test-secret' }),
}));
const mockS3Send = jest.fn();
jest.mock('@aws-sdk/client-s3', () => ({ S3Client: jest.fn(() => ({ send: mockS3Send })), PutObjectCommand: jest.fn() }));

const env = process.env;
const nativeFetch = global.fetch;
let dir: string;
let upstream: jest.Mock;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'artifact-test-'));
  writeFileSync(join(dir, 'run'), 'own-run');
  writeFileSync(join(dir, 'pod'), 'own-pod');
  process.env = { ...env, ADP_AGENT_AUTHORITY_ENABLED: 'true', ADP_GATEWAY_ENDPOINT: 'https://gateway.test/api',
    ADP_RUN_CREDENTIAL_FILE: join(dir, 'run'), ADP_WORKLOAD_TOKEN_FILE: join(dir, 'pod'), ADP_WORKER_AWS_REGION: 'us-east-1',
    AWS_REGION: 'eu-west-2', AGENT_FALLBACK_BUCKET: 'must-not-use', AGENT_RUN_LOGS_BUCKET: 'must-not-use' };
  mockS3Send.mockClear();
  upstream = jest.fn(async (_url, options) => new Response(JSON.stringify({ key: 'runs/own/file', uri: 's3://gateway-bucket/runs/own/file', sha256: createHash('sha256').update(options.body).digest('hex') })));
  global.fetch = upstream;
});
afterEach(() => { global.fetch = nativeFetch; process.env = env; rmSync(dir, { recursive: true, force: true }); });

it('signs exact bytes with current run/pod proofs and no caller destination', async () => {
  expect(await uploadRunArtifact('git-changes', Buffer.from([0, 255, 1]))).toEqual({ key: 'runs/own/file', uri: 's3://gateway-bucket/runs/own/file' });
  const [url, options] = upstream.mock.calls[0];
  expect(String(url)).toBe('https://gateway.test/api/internal/v1/agent/self/artifacts/git-changes');
  expect(options.body).toEqual(Buffer.from([0, 255, 1]));
  const headers = new Headers(options.headers);
  expect(headers.get('authorization')).toContain('PLATFORMTESTKEY/');
  expect(headers.get('authorization')).toContain('/us-east-1/execute-api/');
  expect(headers.get('x-adp-run-credential')).toBe('own-run');
  expect(headers.get('x-adp-workload-token')).toBe('own-pod');
  expect(options.redirect).toBe('error');
  writeFileSync(join(dir, 'run'), 'rotated-run');
  await uploadRunArtifact('spill', 'text');
  expect(new Headers(upstream.mock.calls[1][1].headers).get('x-adp-run-credential')).toBe('rotated-run');
  expect(mockS3Send).not.toHaveBeenCalled();
});

it.each([0, MAX_ARTIFACT_BYTES + 1])('rejects size %i before network', async size => {
  await expect(uploadRunArtifact('spill', Buffer.alloc(size))).rejects.toThrow('maximum 8 MiB');
  expect(upstream).not.toHaveBeenCalled();
});

it.each(['http://gateway.test', 'https://user:pass@gateway.test', 'https://gateway.test?key=other'])('rejects unsafe endpoint %s', async endpoint => {
  process.env.ADP_GATEWAY_ENDPOINT = endpoint;
  await expect(uploadRunArtifact('comment', 'x')).rejects.toThrow('unavailable');
  expect(upstream).not.toHaveBeenCalled();
});

it('rejects unknown kind and missing proof before network', async () => {
  await expect(uploadRunArtifact('../victim' as never, 'x')).rejects.toThrow('unavailable');
  rmSync(join(dir, 'pod'));
  await expect(uploadRunArtifact('comment', 'x')).rejects.toThrow('unavailable');
  expect(upstream).not.toHaveBeenCalled();
});

it.each(['digest', 'oversize', 'redirect', 'exception'])('refuses %s response with a safe error', async failure => {
  upstream.mockImplementation(async () => {
    if (failure === 'exception') throw new Error('sensitive headers');
    if (failure === 'redirect') return new Response('secret', { status: 302 });
    return new Response(failure === 'oversize' ? 'x'.repeat(4097) : JSON.stringify({ key: 'runs/key', uri: 's3://b/runs/key', sha256: 'wrong' }));
  });
  await expect(uploadRunArtifact('comment', 'x')).rejects.toThrow('Own-run artifact upload unavailable');
});

it('both legacy fallback callers stay on the gateway and never try S3 after refusal', async () => {
  upstream.mockResolvedValue(new Response('denied', { status: 403 }));
  expect(await saveToS3Fallback(123, 'arbitrary-label', 'comment')).toBeNull();
  const logger = { error: jest.fn(), info: jest.fn() };
  expect(await new S3Fallback(logger as never, 123).upload('arbitrary-label', 'comment')).toBeNull();
  expect(upstream).toHaveBeenCalledTimes(2);
  expect(mockS3Send).not.toHaveBeenCalled();
});
