/**
 * Unit tests for aws-creds-injector.
 *
 * Issue #586: Make pod-IRSA invisible to the agent's bash shells.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import { createCredsInjector, CredsInjector } from './aws-creds-injector';
import { VaultGatewayClient } from './complex-task-chat/vault/gateway-client';

// Mock fetch globally
const mockFetch = jest.fn();
global.fetch = mockFetch as any;

describe('aws-creds-injector', () => {
  let client: VaultGatewayClient;
  let injector: CredsInjector;

  const MOCK_CREDS_RESPONSE = {
    profile_name: 'adp-aws-test',
    access_key_id: 'ASIAEXAMPLEKEY',
    secret_access_key: 'wJalrXUtnFEMI/SECRET123',
    session_token: 'FwoGZXIvYXdz_SESSION_TOKEN_XYZ',
    expiration: new Date(Date.now() + 60 * 60 * 1000).toISOString(), // 1h from now
    region: 'us-west-2',
    provenance_id: 'prov-test-1',
  };

  // Save original process.env
  const originalEnv = { ...process.env };

  beforeEach(() => {
    mockFetch.mockReset();
    // Simulate pod IRSA env vars
    process.env.AWS_ROLE_ARN = 'arn:aws:iam::879318057152:role/adp-dev-agent-runner-role';
    process.env.AWS_WEB_IDENTITY_TOKEN_FILE = '/var/run/secrets/eks.amazonaws.com/serviceaccount/token';
    process.env.AWS_PROFILE = 'some-profile';

    client = new VaultGatewayClient({
      baseUrl: 'http://gateway:8080',
      apiKey: 'test-api-key',
    });
  });

  afterEach(() => {
    // Restore original process.env
    process.env = { ...originalEnv };
  });

  function setupMockSuccess(response = MOCK_CREDS_RESPONSE): void {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => response,
    });
  }

  function setupMockNotFound(): void {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      text: async () => '{"error":"not_found"}',
    });
  }

  describe('getScopedEnv()', () => {
    it('strips AWS_ROLE_ARN, AWS_WEB_IDENTITY_TOKEN_FILE, AWS_PROFILE from returned env', async () => {
      setupMockSuccess();
      injector = createCredsInjector({
        userId: 'user-1',
        agentId: 'developer',
        taskId: 'task-1',
        vaultClient: client,
      });

      const env = await injector.getScopedEnv();

      expect(env.AWS_ROLE_ARN).toBeUndefined();
      expect(env.AWS_WEB_IDENTITY_TOKEN_FILE).toBeUndefined();
      expect(env.AWS_PROFILE).toBeUndefined();
    });

    it('injects AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, AWS_REGION, AWS_DEFAULT_REGION', async () => {
      setupMockSuccess();
      injector = createCredsInjector({
        userId: 'user-1',
        agentId: 'developer',
        taskId: 'task-1',
        vaultClient: client,
      });

      const env = await injector.getScopedEnv();

      expect(env.AWS_ACCESS_KEY_ID).toBe('ASIAEXAMPLEKEY');
      expect(env.AWS_SECRET_ACCESS_KEY).toBe('wJalrXUtnFEMI/SECRET123');
      expect(env.AWS_SESSION_TOKEN).toBe('FwoGZXIvYXdz_SESSION_TOKEN_XYZ');
      expect(env.AWS_REGION).toBe('us-west-2');
      expect(env.AWS_DEFAULT_REGION).toBe('us-west-2');
    });

    it('does NOT modify process.env (parent IRSA intact)', async () => {
      // CRITICAL: this is the single most important regression guard
      process.env.AWS_ROLE_ARN = 'test-irsa-role-arn';
      process.env.AWS_WEB_IDENTITY_TOKEN_FILE = '/path/to/token';

      setupMockSuccess();
      injector = createCredsInjector({
        userId: 'user-1',
        agentId: 'developer',
        taskId: 'task-1',
        vaultClient: client,
      });

      await injector.getScopedEnv();

      // Parent process env must be unchanged
      expect(process.env.AWS_ROLE_ARN).toBe('test-irsa-role-arn');
      expect(process.env.AWS_WEB_IDENTITY_TOKEN_FILE).toBe('/path/to/token');
    });

    it('when no credential connected, returns env with IRSA stripped but no AWS creds injected', async () => {
      setupMockNotFound();
      injector = createCredsInjector({
        userId: 'user-no-creds',
        agentId: 'developer',
        taskId: 'task-2',
        vaultClient: client,
      });

      const env = await injector.getScopedEnv();

      // IRSA stripped
      expect(env.AWS_ROLE_ARN).toBeUndefined();
      expect(env.AWS_WEB_IDENTITY_TOKEN_FILE).toBeUndefined();
      expect(env.AWS_PROFILE).toBeUndefined();
      // No creds injected
      expect(env.AWS_ACCESS_KEY_ID).toBeUndefined();
      expect(env.AWS_SECRET_ACCESS_KEY).toBeUndefined();
      expect(env.AWS_SESSION_TOKEN).toBeUndefined();
    });

    it('triggers re-assume when cached creds are <5min from expiry', async () => {
      // Provide expiring creds (3 min) — on first getScopedEnv the injector
      // initializes (fetch 1) then immediately detects expiry and refreshes (fetch 2).
      const expiringResponse = {
        ...MOCK_CREDS_RESPONSE,
        access_key_id: 'ASIAEXPIRINGKEY',
        expiration: new Date(Date.now() + 3 * 60 * 1000).toISOString(), // 3 min
      };
      const freshResponse = {
        ...MOCK_CREDS_RESPONSE,
        access_key_id: 'ASIAREFRESHEDKEY',
        expiration: new Date(Date.now() + 60 * 60 * 1000).toISOString(), // 1h
      };
      setupMockSuccess(expiringResponse);
      setupMockSuccess(freshResponse);

      injector = createCredsInjector({
        userId: 'user-1',
        agentId: 'developer',
        taskId: 'task-3',
        vaultClient: client,
      });

      const env = await injector.getScopedEnv();
      // Two fetches: init + refresh
      expect(mockFetch).toHaveBeenCalledTimes(2);
      // Returns the refreshed creds
      expect(env.AWS_ACCESS_KEY_ID).toBe('ASIAREFRESHEDKEY');
    });

    it('does NOT trigger re-assume when cached creds are healthy', async () => {
      setupMockSuccess(); // 1h from now
      injector = createCredsInjector({
        userId: 'user-1',
        agentId: 'developer',
        taskId: 'task-4',
        vaultClient: client,
      });

      await injector.getScopedEnv();
      expect(mockFetch).toHaveBeenCalledTimes(1);

      // Second call — creds still healthy, no extra fetch
      const env = await injector.getScopedEnv();
      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(env.AWS_ACCESS_KEY_ID).toBe('ASIAEXAMPLEKEY');
    });

    it('preserves non-AWS env vars from process.env', async () => {
      process.env.HOME = '/home/testuser';
      process.env.PATH = '/usr/bin:/bin';
      process.env.ANTHROPIC_MODEL = 'claude-sonnet-4-6';

      setupMockSuccess();
      injector = createCredsInjector({
        userId: 'user-1',
        agentId: 'developer',
        taskId: 'task-5',
        vaultClient: client,
      });

      const env = await injector.getScopedEnv();

      expect(env.HOME).toBe('/home/testuser');
      expect(env.PATH).toBe('/usr/bin:/bin');
      expect(env.ANTHROPIC_MODEL).toBe('claude-sonnet-4-6');
    });
  });

  describe('hasCredential()', () => {
    it('returns true before initialization (optimistic)', () => {
      setupMockSuccess();
      injector = createCredsInjector({
        userId: 'user-1',
        agentId: 'developer',
        taskId: 'task-6',
        vaultClient: client,
      });

      // Before any getScopedEnv call
      expect(injector.hasCredential()).toBe(true);
    });

    it('returns true after successful assume', async () => {
      setupMockSuccess();
      injector = createCredsInjector({
        userId: 'user-1',
        agentId: 'developer',
        taskId: 'task-7',
        vaultClient: client,
      });

      await injector.getScopedEnv();
      expect(injector.hasCredential()).toBe(true);
    });

    it('returns false when no credential is connected', async () => {
      setupMockNotFound();
      injector = createCredsInjector({
        userId: 'user-no-aws',
        agentId: 'developer',
        taskId: 'task-8',
        vaultClient: client,
      });

      await injector.getScopedEnv();
      expect(injector.hasCredential()).toBe(false);
    });
  });

  describe('task isolation', () => {
    it('two injectors for different users return different creds', async () => {
      // User A
      setupMockSuccess({
        ...MOCK_CREDS_RESPONSE,
        access_key_id: 'ASIA_USER_A',
        region: 'us-east-1',
      });
      const injectorA = createCredsInjector({
        userId: 'user-A',
        agentId: 'developer',
        taskId: 'task-A',
        vaultClient: client,
      });

      // User B
      setupMockSuccess({
        ...MOCK_CREDS_RESPONSE,
        access_key_id: 'ASIA_USER_B',
        region: 'eu-west-1',
      });
      const injectorB = createCredsInjector({
        userId: 'user-B',
        agentId: 'developer',
        taskId: 'task-B',
        vaultClient: client,
      });

      const envA = await injectorA.getScopedEnv();
      const envB = await injectorB.getScopedEnv();

      expect(envA.AWS_ACCESS_KEY_ID).toBe('ASIA_USER_A');
      expect(envA.AWS_REGION).toBe('us-east-1');
      expect(envB.AWS_ACCESS_KEY_ID).toBe('ASIA_USER_B');
      expect(envB.AWS_REGION).toBe('eu-west-1');
    });

    it('uses closure-scoped state (no module-level mutable state)', async () => {
      // Verify that creating a new injector doesn't share state with prior ones
      setupMockSuccess({
        ...MOCK_CREDS_RESPONSE,
        access_key_id: 'ASIA_FIRST',
      });
      const first = createCredsInjector({
        userId: 'user-1',
        agentId: 'developer',
        taskId: 'task-first',
        vaultClient: client,
      });
      await first.getScopedEnv();

      // Second injector with failed assume
      setupMockNotFound();
      const second = createCredsInjector({
        userId: 'user-2',
        agentId: 'developer',
        taskId: 'task-second',
        vaultClient: client,
      });
      await second.getScopedEnv();

      // First still has creds, second doesn't — no contamination
      expect(first.hasCredential()).toBe(true);
      expect(second.hasCredential()).toBe(false);
    });
  });
});
