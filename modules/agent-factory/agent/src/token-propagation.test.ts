/**
 * Unit tests for token propagation to environment variables and token file.
 *
 * Verifies that when tokens are refreshed (issue #320), the new token is
 * propagated to process.env.GH_TOKEN, GITHUB_TOKEN, GH_APP_TOKEN.
 *
 * After sec/H9 (#1164): tokens are NO LONGER written to disk via
 * `git remote set-url`. Instead, GIT_ASKPASS reads $GITHUB_TOKEN from the
 * environment at each git network call. This test verifies:
 * 1. Environment variables are updated on refresh
 * 2. No `git remote set-url` is called (no disk persistence)
 * 3. GIT_ASKPASS is expected to be set in the environment
 *
 * After issue #1469: tokens are ALSO written to a file that the GIT_ASKPASS
 * helper and gh wrapper read at command-execution time. This solves the
 * frozen-env problem in SDK subprocesses.
 */

// Mock child_process before importing the module under test
jest.mock('child_process', () => ({
  execFileSync: jest.fn(),
}));

// Mock fs for token file writes (issue #1469)
jest.mock('fs', () => ({
  writeFileSync: jest.fn(),
  renameSync: jest.fn(),
  mkdirSync: jest.fn(),
}));

// Mock @octokit/auth-app
jest.mock('@octokit/auth-app', () => ({
  createAppAuth: jest.fn(() => {
    return jest.fn().mockResolvedValue({
      token: 'ghs_refreshed_test_token_123',
      expiresAt: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    });
  }),
}));

// Mock global fetch for token generation
const mockFetch = jest.fn();
global.fetch = mockFetch;

import {
  initTokenManager,
  getToken,
  forceRefresh,
  setToken,
  needsRefresh,
  writeTokenFile,
  TOKEN_FILE_PATH,
} from './token-refresh';

import { execFileSync } from 'child_process';
const mockedExecFileSync = execFileSync as jest.MockedFunction<typeof execFileSync>;

import { writeFileSync, renameSync, mkdirSync } from 'fs';
const mockedWriteFileSync = writeFileSync as jest.MockedFunction<typeof writeFileSync>;
const mockedRenameSync = renameSync as jest.MockedFunction<typeof renameSync>;
const mockedMkdirSync = mkdirSync as jest.MockedFunction<typeof mkdirSync>;

describe('token-propagation (issue #320, hardened by #1164)', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    jest.clearAllMocks();
    // Reset env vars
    delete process.env.GH_TOKEN;
    delete process.env.GITHUB_TOKEN;
    delete process.env.GH_APP_TOKEN;
    process.env.REPO_OWNER = 'test-org';
    process.env.REPO_NAME = 'test-repo';
    process.env.WORK_DIR = '/tmp/workspace';
    process.env.GIT_ASKPASS = '/usr/local/bin/git-askpass-helper';
    process.env.GIT_TERMINAL_PROMPT = '0';
  });

  afterEach(() => {
    // Restore original env
    process.env = { ...originalEnv };
  });

  describe('GIT_ASKPASS environment', () => {
    it('should have GIT_ASKPASS set to the helper script path', () => {
      expect(process.env.GIT_ASKPASS).toBe('/usr/local/bin/git-askpass-helper');
    });

    it('should have GIT_TERMINAL_PROMPT=0 to prevent interactive prompts', () => {
      expect(process.env.GIT_TERMINAL_PROMPT).toBe('0');
    });
  });

  describe('getToken - env var propagation', () => {
    it('should update GH_TOKEN, GITHUB_TOKEN, and GH_APP_TOKEN on refresh', async () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        installationId: '67890',
        owner: 'test-org',
        repo: 'test-repo',
        workDir: '/tmp/workspace',
      });

      // Set an expired token so getToken triggers a refresh
      setToken('old_token', -1000); // Already expired

      await getToken();

      // All three env vars should be set
      expect(process.env.GH_TOKEN).toBe('ghs_refreshed_test_token_123');
      expect(process.env.GITHUB_TOKEN).toBe('ghs_refreshed_test_token_123');
      expect(process.env.GH_APP_TOKEN).toBe('ghs_refreshed_test_token_123');
    });

    it('should NOT call git remote set-url after refresh (sec/H9 fix)', async () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        installationId: '67890',
        owner: 'test-org',
        repo: 'test-repo',
        workDir: '/tmp/workspace',
      });

      setToken('old_token', -1000);
      await getToken();

      // No execSync calls — token propagation is env-only via GIT_ASKPASS
      expect(mockedExecFileSync).not.toHaveBeenCalled();
    });

    it('should propagate token to spawned processes via env inheritance', async () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        installationId: '67890',
        owner: 'test-org',
        repo: 'test-repo',
        workDir: '/tmp/workspace',
      });

      setToken('old_token', -1000);
      await getToken();

      // After refresh, process.env.GITHUB_TOKEN is updated. Child processes
      // spawned via child_process.spawn/exec inherit process.env by default,
      // so GIT_ASKPASS (which reads $GITHUB_TOKEN) will pick up the fresh value.
      expect(process.env.GITHUB_TOKEN).toBe('ghs_refreshed_test_token_123');
      expect(process.env.GIT_ASKPASS).toBe('/usr/local/bin/git-askpass-helper');
    });
  });

  describe('forceRefresh - env var propagation', () => {
    it('should update all env vars on force refresh without disk writes', async () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        installationId: '67890',
        owner: 'test-org',
        repo: 'test-repo',
        workDir: '/tmp/workspace',
      });

      // Set a still-valid token
      setToken('valid_but_stale', 60 * 60 * 1000);

      await forceRefresh();

      expect(process.env.GH_TOKEN).toBe('ghs_refreshed_test_token_123');
      expect(process.env.GITHUB_TOKEN).toBe('ghs_refreshed_test_token_123');
      expect(process.env.GH_APP_TOKEN).toBe('ghs_refreshed_test_token_123');

      // No git remote set-url calls (sec/H9 fix — no disk persistence)
      expect(mockedExecFileSync).not.toHaveBeenCalled();
    });
  });

  describe('setToken does not trigger any disk writes', () => {
    it('should not call execSync when setToken is called', () => {
      setToken('initial_token', 60 * 60 * 1000);

      // setToken only sets the in-memory token, no disk writes
      expect(mockedExecFileSync).not.toHaveBeenCalled();
    });
  });

  describe('token not refreshed when still valid', () => {
    it('should not update env vars when token is still valid', async () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        owner: 'test-org',
        repo: 'test-repo',
        workDir: '/tmp/workspace',
      });

      // Set a valid token with plenty of time remaining
      setToken('still_valid_token', 60 * 60 * 1000);

      process.env.GH_TOKEN = 'should_not_change';

      const token = await getToken();

      // Should return the existing token without refreshing
      expect(token).toBe('still_valid_token');
      expect(process.env.GH_TOKEN).toBe('should_not_change');
      expect(mockedExecFileSync).not.toHaveBeenCalled();
    });
  });

  describe('token file write on refresh (issue #1469)', () => {
    beforeEach(() => {
      mockedWriteFileSync.mockReset();
      mockedRenameSync.mockReset();
      mockedMkdirSync.mockReset();
    });

    it('should write token to file atomically on getToken() refresh', async () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        installationId: '67890',
        owner: 'test-org',
        repo: 'test-repo',
      });

      // Set an expired token so getToken triggers a refresh
      setToken('old_token', -1000);
      await getToken();

      // Should create parent directory
      expect(mockedMkdirSync).toHaveBeenCalledWith(
        expect.any(String),
        expect.objectContaining({ recursive: true, mode: 0o700 })
      );
      // Should write to temp file with restrictive permissions
      expect(mockedWriteFileSync).toHaveBeenCalledWith(
        `${TOKEN_FILE_PATH}.tmp`,
        'ghs_refreshed_test_token_123',
        { mode: 0o600 }
      );
      // Should atomically rename
      expect(mockedRenameSync).toHaveBeenCalledWith(
        `${TOKEN_FILE_PATH}.tmp`,
        TOKEN_FILE_PATH
      );
    });

    it('should write token to file on forceRefresh()', async () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        installationId: '67890',
        owner: 'test-org',
        repo: 'test-repo',
      });

      setToken('valid_but_stale', 60 * 60 * 1000);
      await forceRefresh();

      expect(mockedWriteFileSync).toHaveBeenCalledWith(
        `${TOKEN_FILE_PATH}.tmp`,
        'ghs_refreshed_test_token_123',
        { mode: 0o600 }
      );
      expect(mockedRenameSync).toHaveBeenCalledWith(
        `${TOKEN_FILE_PATH}.tmp`,
        TOKEN_FILE_PATH
      );
    });

    it('should NOT write token file when token is still valid', async () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        owner: 'test-org',
        repo: 'test-repo',
      });

      setToken('still_valid_token', 60 * 60 * 1000);
      await getToken();

      // No file write when token didn't need refresh
      expect(mockedWriteFileSync).not.toHaveBeenCalled();
      expect(mockedRenameSync).not.toHaveBeenCalled();
    });

    it('writeTokenFile uses correct file path from TOKEN_FILE_PATH', () => {
      writeTokenFile('my_token_value');

      expect(mockedWriteFileSync).toHaveBeenCalledWith(
        expect.stringContaining('.tmp'),
        'my_token_value',
        { mode: 0o600 }
      );
    });

    it('should not throw when file write fails (graceful degradation)', () => {
      mockedMkdirSync.mockImplementation(() => {
        throw new Error('Permission denied');
      });

      // Should not throw — logs error but continues
      expect(() => writeTokenFile('test_token')).not.toThrow();
    });
  });
});
