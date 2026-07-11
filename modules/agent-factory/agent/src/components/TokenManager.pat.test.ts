/**
 * Tests for TokenManager PAT mode (Issue #3385, A4).
 *
 * Verifies that:
 * 1. ADP_TOKEN_MODE=pat causes TokenManager to adopt env GITHUB_TOKEN
 * 2. No refresh timer is started in PAT mode
 * 3. No App credential loading occurs in PAT mode
 * 4. getToken() returns the env PAT value
 */

import { TokenManager } from './TokenManager';
import { ConfigLoader } from './ConfigLoader';
import { Logger } from './Logger';

// Mock ConfigLoader to track calls
const mockGetSecret = jest.fn();
const mockConfigLoader = {
  getSecret: mockGetSecret,
} as unknown as ConfigLoader;

// Mock Logger
const mockLogger = {
  info: jest.fn(),
  warn: jest.fn(),
  debug: jest.fn(),
  error: jest.fn(),
} as unknown as Logger;

describe('TokenManager — ADP_TOKEN_MODE=pat (Issue #3385, A4)', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    jest.clearAllMocks();
    // Clean up env
    delete process.env.ADP_TOKEN_MODE;
    delete process.env.GITHUB_TOKEN;
    delete process.env.GH_TOKEN;
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it('adopts GITHUB_TOKEN from env when ADP_TOKEN_MODE=pat', async () => {
    process.env.ADP_TOKEN_MODE = 'pat';
    process.env.GITHUB_TOKEN = 'ghp_test_pat_from_entrypoint';

    const tm = new TokenManager(mockConfigLoader, mockLogger);
    await tm.initialize();

    const token = await tm.getToken();
    expect(token).toBe('ghp_test_pat_from_entrypoint');
  });

  it('does NOT call configLoader.getSecret for app-credentials', async () => {
    process.env.ADP_TOKEN_MODE = 'pat';
    process.env.GITHUB_TOKEN = 'ghp_test_pat';

    const tm = new TokenManager(mockConfigLoader, mockLogger);
    await tm.initialize();

    // Should not attempt to load App credentials
    expect(mockGetSecret).not.toHaveBeenCalledWith('app-credentials');
  });

  it('does NOT call configLoader.getSecret for github-pat (ConfigLoader path)', async () => {
    process.env.ADP_TOKEN_MODE = 'pat';
    process.env.GITHUB_TOKEN = 'ghp_test_pat';

    const tm = new TokenManager(mockConfigLoader, mockLogger);
    await tm.initialize();

    // The legacy ConfigLoader-based PAT path should be skipped entirely
    expect(mockGetSecret).not.toHaveBeenCalled();
  });

  it('does NOT start refresh timer in PAT mode', async () => {
    process.env.ADP_TOKEN_MODE = 'pat';
    process.env.GITHUB_TOKEN = 'ghp_test_pat';

    const tm = new TokenManager(mockConfigLoader, mockLogger);
    const startTimerSpy = jest.spyOn(tm, 'startRefreshTimer');
    await tm.initialize();

    expect(startTimerSpy).not.toHaveBeenCalled();
  });

  it('logs initialization with PAT mode message', async () => {
    process.env.ADP_TOKEN_MODE = 'pat';
    process.env.GITHUB_TOKEN = 'ghp_test_pat';

    const tm = new TokenManager(mockConfigLoader, mockLogger);
    await tm.initialize();

    expect(mockLogger.info).toHaveBeenCalledWith(
      expect.stringContaining('ADP_TOKEN_MODE=pat'),
      expect.any(Object),
    );
  });

  it('falls back to GH_TOKEN if GITHUB_TOKEN is empty', async () => {
    process.env.ADP_TOKEN_MODE = 'pat';
    process.env.GITHUB_TOKEN = '';
    process.env.GH_TOKEN = 'ghp_from_gh_token_var';

    const tm = new TokenManager(mockConfigLoader, mockLogger);
    await tm.initialize();

    const token = await tm.getToken();
    expect(token).toBe('ghp_from_gh_token_var');
  });

  it('logs warning if ADP_TOKEN_MODE=pat but no token in env', async () => {
    process.env.ADP_TOKEN_MODE = 'pat';
    // No GITHUB_TOKEN or GH_TOKEN set

    // The fallback path will try ConfigLoader PAT, then App credentials.
    // We only need to verify the warning was logged — the fallback may fail
    // due to mock credentials, which is expected in a unit test.
    mockGetSecret.mockRejectedValueOnce(new Error('no PAT'));
    mockGetSecret.mockRejectedValueOnce(new Error('no App creds'));

    const tm = new TokenManager(mockConfigLoader, mockLogger);

    // initialize will throw because both paths fail, but the warning should be logged
    await expect(tm.initialize()).rejects.toThrow();
    expect(mockLogger.warn).toHaveBeenCalledWith(
      expect.stringContaining('ADP_TOKEN_MODE=pat but no GITHUB_TOKEN'),
      expect.any(Object),
    );
  });

  it('without ADP_TOKEN_MODE, uses legacy ConfigLoader PAT path', async () => {
    // No ADP_TOKEN_MODE set — existing behavior
    mockGetSecret.mockResolvedValueOnce({ token: 'ghp_legacy_pat' });

    const tm = new TokenManager(mockConfigLoader, mockLogger);
    await tm.initialize();

    expect(mockGetSecret).toHaveBeenCalledWith('github-pat');
    const token = await tm.getToken();
    expect(token).toBe('ghp_legacy_pat');
  });
});
