/**
 * Tests for token-refresh.ts — specifically the execWithFreshToken refactor (#1163).
 *
 * Verifies that:
 * 1. execWithFreshToken uses execFileSync with argv array (no shell)
 * 2. Token is refreshed before command execution
 * 3. 401 errors trigger a retry with a fresh token
 * 4. Shell metacharacters in args are treated as literals (not interpreted)
 */

// Mock child_process before importing the module under test
jest.mock('child_process', () => ({
  execFileSync: jest.fn(),
}));

// Mock @octokit/auth-app
jest.mock('@octokit/auth-app', () => ({
  createAppAuth: jest.fn(() => {
    return jest.fn().mockResolvedValue({
      token: 'ghs_fresh_token_abc',
      expiresAt: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    });
  }),
}));

import {
  initTokenManager,
  setToken,
  execWithFreshToken,
} from './token-refresh';

import { execFileSync } from 'child_process';
const mockedExecFileSync = execFileSync as jest.MockedFunction<typeof execFileSync>;

describe('execWithFreshToken — shell injection prevention (#1163)', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    jest.clearAllMocks();
    delete process.env.GH_TOKEN;
    delete process.env.GITHUB_TOKEN;
    delete process.env.GH_APP_TOKEN;

    initTokenManager({
      appId: '12345',
      privateKey: 'fake-key',
      installationId: '67890',
      owner: 'test-org',
      repo: 'test-repo',
    });

    // Set a valid token to avoid triggering actual refresh logic
    setToken('ghs_test_token_valid', 60 * 60 * 1000);

    mockedExecFileSync.mockReturnValue('command output\n');
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  describe('argv-based execution (no shell)', () => {
    it('calls execFileSync with file and args array', async () => {
      await execWithFreshToken('gh', ['issue', 'view', '42']);

      expect(mockedExecFileSync).toHaveBeenCalledTimes(1);
      expect(mockedExecFileSync).toHaveBeenCalledWith(
        'gh',
        ['issue', 'view', '42'],
        expect.objectContaining({ encoding: 'utf-8' })
      );
    });

    it('passes args as separate array elements — NOT as a shell string', async () => {
      await execWithFreshToken('git', ['clone', 'https://github.com/org/repo', '/tmp/dir']);

      const [file, args] = mockedExecFileSync.mock.calls[0];
      expect(file).toBe('git');
      expect(args).toEqual(['clone', 'https://github.com/org/repo', '/tmp/dir']);
    });

    it('treats shell metacharacters in args as literal text', async () => {
      // These would be dangerous if passed to a shell, but execFileSync
      // passes them as literal argv elements
      const maliciousArgs = [
        'issue', 'create',
        '--title', '$(whoami)',
        '--body', '; rm -rf / #',
      ];

      await execWithFreshToken('gh', maliciousArgs);

      const [, args] = mockedExecFileSync.mock.calls[0];
      expect(args).toContain('$(whoami)');
      expect(args).toContain('; rm -rf / #');
    });

    it('trims output', async () => {
      mockedExecFileSync.mockReturnValue('  result with whitespace  \n');

      const result = await execWithFreshToken('gh', ['pr', 'list']);

      expect(result).toBe('result with whitespace');
    });

    it('passes cwd option when provided', async () => {
      await execWithFreshToken('git', ['status'], { cwd: '/workspace' });

      expect(mockedExecFileSync).toHaveBeenCalledWith(
        'git',
        ['status'],
        expect.objectContaining({ cwd: '/workspace' })
      );
    });

    it('merges env overrides with process.env and token', async () => {
      await execWithFreshToken('git', ['push'], { env: { CUSTOM_VAR: 'value' } });

      const opts = mockedExecFileSync.mock.calls[0][2] as { env: Record<string, string> };
      expect(opts.env.CUSTOM_VAR).toBe('value');
      expect(opts.env.GH_TOKEN).toBe('ghs_test_token_valid');
      expect(opts.env.GITHUB_TOKEN).toBe('ghs_test_token_valid');
    });
  });

  describe('token refresh before execution', () => {
    it('includes fresh token in env for the child process', async () => {
      await execWithFreshToken('gh', ['api', '/user']);

      const opts = mockedExecFileSync.mock.calls[0][2] as { env: Record<string, string> };
      expect(opts.env.GH_TOKEN).toBe('ghs_test_token_valid');
      expect(opts.env.GITHUB_TOKEN).toBe('ghs_test_token_valid');
    });

    it('refreshes expired token before execution', async () => {
      // Set an expired token — getToken() will trigger a refresh
      setToken('ghs_expired', -1000);

      await execWithFreshToken('gh', ['issue', 'list']);

      // After refresh, the mock auth returns 'ghs_fresh_token_abc'
      const opts = mockedExecFileSync.mock.calls[0][2] as { env: Record<string, string> };
      expect(opts.env.GH_TOKEN).toBe('ghs_fresh_token_abc');
    });
  });

  describe('401 retry with fresh token', () => {
    it('retries once on 401 error with a refreshed token', async () => {
      const error401 = new Error('Command failed: exit 1') as Error & { stderr: string };
      error401.stderr = 'Bad credentials';

      mockedExecFileSync
        .mockImplementationOnce(() => { throw error401; })
        .mockReturnValueOnce('success after retry\n');

      const result = await execWithFreshToken('gh', ['pr', 'view', '99']);

      expect(result).toBe('success after retry');
      expect(mockedExecFileSync).toHaveBeenCalledTimes(2);

      // Second call should have the refreshed token
      const retryOpts = mockedExecFileSync.mock.calls[1][2] as { env: Record<string, string> };
      expect(retryOpts.env.GH_TOKEN).toBe('ghs_fresh_token_abc');
    });

    it('retries on 401 status code in error message', async () => {
      const error401 = new Error('HTTP 401: Unauthorized');

      mockedExecFileSync
        .mockImplementationOnce(() => { throw error401; })
        .mockReturnValueOnce('retried output\n');

      const result = await execWithFreshToken('gh', ['api', '/repos']);

      expect(result).toBe('retried output');
      expect(mockedExecFileSync).toHaveBeenCalledTimes(2);
    });

    it('does not retry on non-401 errors', async () => {
      const error500 = new Error('Command failed: exit 128');

      mockedExecFileSync.mockImplementationOnce(() => { throw error500; });

      await expect(execWithFreshToken('git', ['push'])).rejects.toThrow('exit 128');
      expect(mockedExecFileSync).toHaveBeenCalledTimes(1);
    });
  });
});
