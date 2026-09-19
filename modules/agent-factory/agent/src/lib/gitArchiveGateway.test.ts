import { createHash, randomBytes } from 'node:crypto';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MAX_ARTIFACT_BYTES, uploadRunArtifact } from './artifactGateway';
import { archiveProtectedGitChanges } from './gitArchiveGateway';

jest.mock('./artifactGateway', () => ({
  ...jest.requireActual('./artifactGateway'), uploadRunArtifact: jest.fn(),
}));

let directory: string;
let archivePath: string;
let uploaded: { kind: string; body: Buffer; uri: string }[];
beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), 'git-archive-gateway-'));
  archivePath = join(directory, 'recovery.tar.gz');
  uploaded = [];
  jest.spyOn(console, 'log').mockImplementation(() => undefined);
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
  (uploadRunArtifact as jest.Mock).mockReset().mockImplementation(async (kind, content) => {
    const body = Buffer.from(content);
    const key = `runs/own/${kind}/${createHash('sha256').update(body).digest('hex')}`;
    const uri = `s3://gateway-bucket/${key}`;
    uploaded.push({ kind, body, uri });
    return { key, uri };
  });
});
afterEach(() => { jest.restoreAllMocks(); rmSync(directory, { recursive: true, force: true }); });
const backup = () => archiveProtectedGitChanges({ archivePath, issueNumber: 5195, timestamp: '2026-09-19T14:00:00Z', files: ['src/work.ts'] });

it.each([3, MAX_ARTIFACT_BYTES, MAX_ARTIFACT_BYTES + 37])('preserves all %i archive bytes with bounded requests and an ordered recovery manifest', async size => {
  const original = randomBytes(size);
  writeFileSync(archivePath, original);
  expect(await backup()).toBe(true);
  const parts = uploaded.filter(item => item.kind === 'git-changes');
  expect(Buffer.concat(parts.map(part => part.body)).equals(original)).toBe(true);
  expect(parts.every(part => part.body.length <= MAX_ARTIFACT_BYTES)).toBe(true);
  const manifest = uploaded.at(-1)!;
  expect(manifest.kind).toBe('git-manifest');
  const text = manifest.body.toString();
  expect(text).toContain('Issue: #5195');
  expect(text).toContain('Timestamp: 2026-09-19T14:00:00Z');
  expect(text).toContain(`Archive SHA256: ${createHash('sha256').update(original).digest('hex')}`);
  parts.forEach((part, index) => expect(text).toContain(`${index + 1}. ${part.uri} (${part.body.length} bytes; SHA256 ${createHash('sha256').update(part.body).digest('hex')})`));
  expect(existsSync(archivePath)).toBe(false);
  expect(console.log).toHaveBeenLastCalledWith(expect.stringContaining('1 changed files saved; recovery manifest:'));
  expect(console.error).not.toHaveBeenCalled();
});

it.each(['first-part', 'later-part', 'manifest'])('reports %s refusal loudly, retains local recovery and never announces success', async failure => {
  writeFileSync(archivePath, Buffer.alloc(MAX_ARTIFACT_BYTES + 1, 8));
  const ordinary = (uploadRunArtifact as jest.Mock).getMockImplementation()!;
  let count = 0;
  (uploadRunArtifact as jest.Mock).mockImplementation(async (kind, body) => {
    count++;
    if ((failure === 'first-part' && count === 1) || (failure === 'later-part' && count === 2) || (failure === 'manifest' && kind === 'git-manifest')) {
      throw new Error('Authorization: secret-value');
    }
    return ordinary(kind, body);
  });
  expect(await backup()).toBe(false);
  expect(existsSync(archivePath)).toBe(true);
  expect(console.error).toHaveBeenCalledWith(expect.stringContaining('changed files could NOT be preserved'));
  const output = JSON.stringify([(console.log as jest.Mock).mock.calls, (console.error as jest.Mock).mock.calls]);
  expect(output).not.toContain('secret-value');
  expect(output).not.toContain('changed files saved;');
  uploaded.forEach(part => expect(output).toContain(part.uri));
});

it('retains duplicate content-addressed parts in order instead of deduplicating archive bytes', async () => {
  writeFileSync(archivePath, Buffer.alloc(2 * MAX_ARTIFACT_BYTES, 42));
  expect(await backup()).toBe(true);
  expect(uploaded[0].uri).toBe(uploaded[1].uri);
  expect(uploaded[2].body.toString()).toContain(`1. ${uploaded[0].uri}`);
  expect(uploaded[2].body.toString()).toContain(`2. ${uploaded[0].uri}`);
});
