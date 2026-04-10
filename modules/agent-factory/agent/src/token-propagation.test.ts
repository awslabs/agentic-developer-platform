/**
 * Unit tests for token propagation to environment variables and git remote URL.
 *
 * Verifies that when tokens are refreshed (issue #320), the new token is
 * propagated to:
 * 1. process.env.GH_TOKEN, GITHUB_TOKEN, GH_APP_TOKEN
 * 2. The git remote URL via `git remote set-url origin`
 */

import { execSync } from 'child_process';

// Mock child_process before importing the module under test
jest.mock('child_process', () => ({
  execSync: jest.fn(),
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
  updateGitRemoteToken,
  needsRefresh,
} from './token-refresh';

const mockedExecSync = execSync as jest.MockedFunction<typeof execSync>;

describe('token-propagation (issue #320)', () => {
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
  });

  afterEach(() => {
    // Restore original env
    process.env = { ...originalEnv };
  });

  describe('updateGitRemoteToken', () => {
    it('should run git remote set-url with the correct URL', () => {
      updateGitRemoteToken('ghs_new_token_abc', {
        owner: 'my-org',
        repo: 'my-repo',
        workDir: '/work/dir',
      });

      expect(mockedExecSync).toHaveBeenCalledWith(
        'git remote set-url origin https://x-access-token:ghs_new_token_abc@github.com/my-org/my-repo.git',
        expect.objectContaining({
          cwd: '/work/dir',
          stdio: 'ignore',
          timeout: 10000,
        }),
      );
    });

    it('should fall back to env vars when options not provided', () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        owner: 'env-org',
        repo: 'env-repo',
        workDir: '/env/workspace',
      });

      updateGitRemoteToken('ghs_token_xyz');

      expect(mockedExecSync).toHaveBeenCalledWith(
        expect.stringContaining('https://x-access-token:ghs_token_xyz@github.com/env-org/env-repo.git'),
        expect.objectContaining({ cwd: '/env/workspace' }),
      );
    });

    it('should fall back to process.env when config has no repo/workDir', () => {
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        owner: 'config-org',
      });

      // process.env.REPO_NAME and WORK_DIR are set in beforeEach
      updateGitRemoteToken('ghs_fallback_token');

      expect(mockedExecSync).toHaveBeenCalledWith(
        expect.stringContaining('https://x-access-token:ghs_fallback_token@github.com/config-org/test-repo.git'),
        expect.objectContaining({ cwd: '/tmp/workspace' }),
      );
    });

    it('should not throw if git remote set-url fails', () => {
      mockedExecSync.mockImplementation(() => {
        throw new Error('fatal: not a git repository');
      });

      // Should not throw
      expect(() => {
        updateGitRemoteToken('ghs_token', {
          owner: 'org',
          repo: 'repo',
          workDir: '/dir',
        });
      }).not.toThrow();
    });

    it('should skip if owner/repo/workDir are missing', () => {
      delete process.env.REPO_OWNER;
      delete process.env.REPO_NAME;
      delete process.env.WORK_DIR;

      // Re-init without repo/workDir
      initTokenManager({
        appId: '12345',
        privateKey: 'fake-key',
        owner: '',
      });

      updateGitRemoteToken('ghs_token');

      expect(mockedExecSync).not.toHaveBeenCalled();
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

    it('should call updateGitRemoteToken after refresh', async () => {
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

      // Should have called git remote set-url
      expect(mockedExecSync).toHaveBeenCalledWith(
        expect.stringContaining('git remote set-url origin'),
        expect.any(Object),
      );
    });
  });

  describe('forceRefresh - env var propagation', () => {
    it('should update all env vars and git remote on force refresh', async () => {
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

      // Git remote should also be updated
      expect(mockedExecSync).toHaveBeenCalledWith(
        expect.stringContaining('git remote set-url origin'),
        expect.any(Object),
      );
    });
  });

  describe('setToken does not trigger git remote update', () => {
    it('should not update git remote when setToken is called (no refresh)', () => {
      setToken('initial_token', 60 * 60 * 1000);

      // setToken only sets the in-memory token, no git remote update
      expect(mockedExecSync).not.toHaveBeenCalled();
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
      expect(mockedExecSync).not.toHaveBeenCalled();
    });
  });
});
