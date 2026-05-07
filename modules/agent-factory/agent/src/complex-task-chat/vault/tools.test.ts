/**
 * Unit tests for vault MCP tools.
 *
 * Issue #137: Vault Phase 4
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import { vaultToolsForTurn, SanitizableAgentTool } from './tools';
import { Scrubber } from '../context/scrubber';
import { VaultGatewayClient } from './gateway-client';

// Mock fetch globally
const mockFetch = jest.fn();
global.fetch = mockFetch as any;

describe('vault tools', () => {
  let scrubber: Scrubber;
  let client: VaultGatewayClient;
  let tools: SanitizableAgentTool[];

  beforeEach(() => {
    scrubber = new Scrubber();
    client = new VaultGatewayClient({
      baseUrl: 'http://gateway:8080',
      apiKey: 'test-api-key',
    });
    tools = vaultToolsForTurn({
      userId: 'user-123',
      agentId: 'developer',
      taskId: 'task-456',
      scrubber,
      client,
    });
    mockFetch.mockReset();
  });

  function findTool(name: string): SanitizableAgentTool {
    const t = tools.find(t => t.name === name);
    if (!t) throw new Error(`Tool ${name} not found`);
    return t;
  }

  describe('list_user_credentials', () => {
    it('returns credential metadata without secret values', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => [
          { id: 'cred-1', service: 'github', label: 'Personal', credential_type: 'oauth_token', expires_at: null, last_used_at: null },
          { id: 'cred-2', service: 'jira', label: 'Work', credential_type: 'api_key', expires_at: null, last_used_at: null },
        ],
      });

      const tool = findTool('list_user_credentials');
      const result = await tool.handler({});
      const parsed = JSON.parse(result.content[0].text);

      expect(parsed.credentials).toHaveLength(2);
      expect(parsed.credentials[0].service).toBe('github');
      expect(parsed.credentials[0]).not.toHaveProperty('secret_arn');
      expect(parsed.credentials[0]).not.toHaveProperty('value');
    });

    it('calls gateway with correct user_id from closure', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => [],
      });

      const tool = findTool('list_user_credentials');
      await tool.handler({});

      expect(mockFetch).toHaveBeenCalledWith(
        expect.stringContaining('user_id=user-123'),
        expect.objectContaining({ method: 'GET' }),
      );
    });

    it('inputSummarySanitizer returns input unchanged (no secrets)', () => {
      const tool = findTool('list_user_credentials');
      const input = {};
      expect(tool.inputSummarySanitizer!(input)).toEqual(input);
    });
  });

  describe('http_request_with_credential', () => {
    it('calls proxy-request with closure-injected userId', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          status: 200,
          headers: { 'content-type': 'application/json' },
          body: '{"login":"test"}',
          provenance_id: 'prov-1',
        }),
      });

      const tool = findTool('http_request_with_credential');
      const result = await tool.handler({
        service: 'github',
        method: 'GET',
        url: 'https://api.github.com/user',
      });

      // Verify the closure-injected userId was used
      const fetchCall = mockFetch.mock.calls[0];
      const body = JSON.parse(fetchCall[1].body);
      expect(body.user_id).toBe('user-123');
      expect(body.agent_id).toBe('developer');
      expect(body.task_id).toBe('task-456');
      expect(body.service).toBe('github');

      // Verify response
      const parsed = JSON.parse(result.content[0].text);
      expect(parsed.status).toBe(200);
      expect(parsed.provenance_id).toBe('prov-1');
    });

    it('inputSummarySanitizer strips body and headers', () => {
      const tool = findTool('http_request_with_credential');
      const input = {
        service: 'github',
        label: 'personal',
        method: 'POST',
        url: 'https://api.github.com/repos',
        headers: { Authorization: 'Bearer secret' },
        body: '{"name":"repo"}',
      };
      const sanitized = tool.inputSummarySanitizer!(input);
      expect(sanitized).toEqual({
        service: 'github',
        label: 'personal',
        method: 'POST',
        url: 'https://api.github.com/repos',
      });
      expect(sanitized).not.toHaveProperty('headers');
      expect(sanitized).not.toHaveProperty('body');
    });

    it('cannot override user_id via input manipulation', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({ status: 200, headers: {}, body: '', provenance_id: 'x' }),
      });

      const tool = findTool('http_request_with_credential');
      await tool.handler({
        service: 'github',
        method: 'GET',
        url: 'https://example.com',
        user_id: 'attacker-id', // attempt to override
      });

      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      // Closure-injected userId wins — attacker's override ignored
      expect(body.user_id).toBe('user-123');
    });
  });

  describe('materialize_user_credential', () => {
    it('returns presigned URL info, not raw credential', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          materialize_url: 'https://s3.presigned/vault/materialize/xyz',
          expires_at: '2026-05-05T15:00:00Z',
          provenance_id: 'prov-2',
        }),
      });

      const tool = findTool('materialize_user_credential');
      const result = await tool.handler({ service: 'ssh-key-prod' });
      const parsed = JSON.parse(result.content[0].text);

      expect(parsed.materialize_url).toContain('presigned');
      expect(parsed.expires_at).toBeDefined();
      expect(parsed).not.toHaveProperty('value');
    });

    it('inputSummarySanitizer passes through (no secrets in input)', () => {
      const tool = findTool('materialize_user_credential');
      const input = { service: 'ssh-key', label: 'prod' };
      expect(tool.inputSummarySanitizer!(input)).toEqual(input);
    });
  });

  describe('get_user_credential_raw', () => {
    it('registers value with scrubber BEFORE returning', async () => {
      const secretValue = 'ghp_verySecretTokenValue123456';
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          value: secretValue,
          credential_type: 'oauth_token',
          provenance_id: 'prov-3',
        }),
      });

      const tool = findTool('get_user_credential_raw');
      await tool.handler({ service: 'github', label: 'personal' });

      // Verify scrubber was registered
      expect(scrubber.scrub(`token: ${secretValue}`)).toBe(
        'token: <<redacted:github:personal>>',
      );
    });

    it('returns value in response', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          value: 'raw-secret-value-12345',
          credential_type: 'api_key',
          provenance_id: 'prov-4',
        }),
      });

      const tool = findTool('get_user_credential_raw');
      const result = await tool.handler({ service: 'jira' });
      const parsed = JSON.parse(result.content[0].text);

      expect(parsed.value).toBe('raw-secret-value-12345');
      expect(parsed.credential_type).toBe('api_key');
    });

    it('uses default label in replacement when label not provided', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          value: 'another-secret-val-xyz',
          credential_type: 'oauth_token',
          provenance_id: 'prov-5',
        }),
      });

      const tool = findTool('get_user_credential_raw');
      await tool.handler({ service: 'github' });

      expect(scrubber.scrub('has another-secret-val-xyz inside')).toBe(
        'has <<redacted:github:default>> inside',
      );
    });

    it('inputSummarySanitizer does not include value field', () => {
      const tool = findTool('get_user_credential_raw');
      const input = { service: 'github', label: 'x', purpose: 'auth' };
      const sanitized = tool.inputSummarySanitizer!(input);
      expect(sanitized).toEqual({ service: 'github', label: 'x', purpose: 'auth' });
      expect(sanitized).not.toHaveProperty('value');
    });
  });

  describe('assume_user_aws_role', () => {
    it('returns only profile_name, expiration, region — no raw creds', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          profile_name: 'adp-aws-prod',
          access_key_id: 'ASIAEXAMPLE',
          secret_access_key: 'wJalrXUtnFEMI/SECRET',
          session_token: 'FwoGZXIvYXdz_SESSION_TOKEN',
          expiration: '2026-05-07T17:00:00Z',
          region: 'us-west-2',
          provenance_id: 'prov-assume-1',
        }),
      });

      const tool = findTool('assume_user_aws_role');
      const result = await tool.handler({ service: 'aws', label: 'prod' });
      const parsed = JSON.parse(result.content[0].text);

      // Only safe metadata returned to the agent.
      expect(parsed.profile_name).toBe('adp-aws-prod');
      expect(parsed.expiration).toBe('2026-05-07T17:00:00Z');
      expect(parsed.region).toBe('us-west-2');
      // Raw creds NOT in output.
      expect(parsed).not.toHaveProperty('access_key_id');
      expect(parsed).not.toHaveProperty('secret_access_key');
      expect(parsed).not.toHaveProperty('session_token');
    });

    it('registers secret_access_key and session_token with scrubber', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          profile_name: 'adp-aws-staging',
          access_key_id: 'ASIASTAGING',
          secret_access_key: 'stagingSecretKey123',
          session_token: 'stagingSessionToken456',
          expiration: '2026-05-07T18:00:00Z',
          region: 'eu-west-1',
          provenance_id: 'prov-assume-2',
        }),
      });

      const tool = findTool('assume_user_aws_role');
      await tool.handler({ service: 'aws', label: 'staging' });

      // Scrubber should redact both secret values.
      expect(scrubber.scrub('key: stagingSecretKey123')).toBe(
        'key: <<redacted:aws:staging:secret>>',
      );
      expect(scrubber.scrub('token: stagingSessionToken456')).toBe(
        'token: <<redacted:aws:staging:session>>',
      );
    });

    it('uses closure-injected userId — cannot be overridden by input', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          profile_name: 'adp-aws-default',
          access_key_id: 'ASIATEST',
          secret_access_key: 'testSecret',
          session_token: 'testSession',
          expiration: '2026-05-07T19:00:00Z',
          region: 'us-east-1',
          provenance_id: 'prov-assume-3',
        }),
      });

      const tool = findTool('assume_user_aws_role');
      await tool.handler({
        service: 'aws',
        user_id: 'attacker-id', // attempt to override
      });

      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      expect(body.user_id).toBe('user-123');
    });

    it('uses default label in scrubber replacement when label not provided', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          profile_name: 'adp-aws-default',
          access_key_id: 'ASIADEFAULT',
          secret_access_key: 'defaultSecretXYZ',
          session_token: 'defaultSessionXYZ',
          expiration: '2026-05-07T20:00:00Z',
          region: 'us-east-1',
          provenance_id: 'prov-assume-4',
        }),
      });

      const tool = findTool('assume_user_aws_role');
      await tool.handler({ service: 'aws' });

      expect(scrubber.scrub('defaultSecretXYZ')).toBe('<<redacted:aws:default:secret>>');
      expect(scrubber.scrub('defaultSessionXYZ')).toBe('<<redacted:aws:default:session>>');
    });

    it('inputSummarySanitizer returns input unchanged (no secrets in input)', () => {
      const tool = findTool('assume_user_aws_role');
      const input = { service: 'aws', label: 'prod', purpose: 'deploy' };
      expect(tool.inputSummarySanitizer!(input)).toEqual(input);
    });
  });

  describe('tools gating', () => {
    it('returns 5 tools when all config provided', () => {
      expect(tools).toHaveLength(5);
      const names = tools.map(t => t.name);
      expect(names).toContain('list_user_credentials');
      expect(names).toContain('http_request_with_credential');
      expect(names).toContain('materialize_user_credential');
      expect(names).toContain('assume_user_aws_role');
      expect(names).toContain('get_user_credential_raw');
    });
  });

  describe('error handling', () => {
    it('returns isError: true when gateway call fails', async () => {
      mockFetch.mockResolvedValueOnce({
        ok: false,
        status: 404,
        text: async () => '{"error":"not_found"}',
      });

      const tool = findTool('http_request_with_credential');
      const result = await tool.handler({
        service: 'github',
        method: 'GET',
        url: 'https://example.com',
      });

      expect(result.isError).toBe(true);
      expect(result.content[0].text).toContain('failed');
    });
  });
});
