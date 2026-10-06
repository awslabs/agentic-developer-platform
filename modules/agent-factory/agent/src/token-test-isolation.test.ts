import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

const root = resolve(__dirname, '..');
const jestCli = require.resolve('jest/bin/jest');
const runtimeToken = 'dummy-runtime-token';

describe('Jest token isolation in an agent workspace', () => {
  test.each([
    { inherited: true, fail: false },
    { inherited: false, fail: false },
    { inherited: true, fail: true },
  ])('protects credentials and cleans up (inherited=$inherited, failure=$fail)', ({ inherited, fail }) => {
    const directory = mkdtempSync(join(tmpdir(), 'adp-token-isolation-check-'));
    const tokenFile = join(directory, 'runtime-token');
    const receipt = join(directory, 'receipt');
    try {
      writeFileSync(tokenFile, runtimeToken, { mode: 0o600 });
      // Never forward the host's real credentials or broker configuration.
      const env = {
        PATH: process.env.PATH,
        HOME: directory,
        TMPDIR: directory,
        CI: 'true',
        TOKEN_ISOLATION_ROOT: directory,
        TOKEN_ISOLATION_RUNTIME_FILE: tokenFile,
        TOKEN_ISOLATION_RECEIPT: receipt,
        TOKEN_ISOLATION_FAIL: String(fail),
        ...(inherited ? { ADP_TOKEN_FILE: tokenFile } : {}),
      };
      // Reproduce the original destructive suite with only dummy credentials.
      // In the unset-path case run only the guarded fixture, so even a regression
      // cannot write to the real /tmp/.adp-gh-token during this test.
      const files = ['tests/fixtures/token-file.fixture.js'];
      if (inherited && !fail) files.push('src/utils/ghPost.test.ts');
      const result = spawnSync(process.execPath, [jestCli, '--runInBand', '--runTestsByPath', ...files,
        '--roots', 'src', 'tests/fixtures', '--testMatch', '**/ghPost.test.ts', '**/token-file.fixture.js'], {
        cwd: root, env, encoding: 'utf8', timeout: 120_000,
      });
      expect(result.error).toBeUndefined();
      expect({ status: result.status, output: result.status === (fail ? 1 : 0) ? '' : result.stdout + result.stderr })
        .toEqual({ status: fail ? 1 : 0, output: '' });
      expect(readFileSync(tokenFile, 'utf8')).toBe(runtimeToken);
      const fixturePath = readFileSync(receipt, 'utf8');
      expect(existsSync(dirname(fixturePath))).toBe(false);
      expect(readdirSync(directory).filter(name => name.startsWith('adp-agent-jest-token-'))).toEqual([]);

      // Exercise both shipped credential readers after the child test process.
      const cli = join(directory, 'gh-stub');
      writeFileSync(cli, '#!/bin/sh\nprintf "%s" "$GH_TOKEN"\n', { mode: 0o700 });
      const readerEnv = { PATH: process.env.PATH, ADP_TOKEN_FILE: tokenFile, ADP_REAL_GH: cli,
        GH_TOKEN: 'dummy-env-fallback', GITHUB_TOKEN: 'dummy-env-fallback' };
      for (const reader of ['gh-wrapper', 'git-askpass-helper']) {
        expect(execFileSync('sh', [join(root, '../agent-worker-image', reader)], { env: readerEnv, encoding: 'utf8' }).trim())
          .toBe(runtimeToken);
      }
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  }, 150_000);
});
