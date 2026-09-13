/**
 * Tests for githubTokenBroker.ts (issue #4272).
 *
 * This client replaces the local mint that required GH_APP_PRIVATE_KEY in the
 * agent process. The properties that matter are the ones that would let the old
 * behavior creep back in, or let a failure pass unnoticed:
 *
 *  - no gateway configured is a THROW, never a silent no-op that would leave the
 *    caller falling back to a local mint
 *  - the request carries no tenant: the gateway derives it, so a hijacked run
 *    cannot name someone else's org
 *  - expires_at is taken from the response and parsed; a missing or garbage
 *    value throws rather than becoming a locally-guessed now+1h
 *  - the SigV4 path is exercised for real (static creds so it is deterministic
 *    in CI, as provenanceClient.test.ts established)
 */

import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fetchBrokeredToken, isBrokerEnabled } from './githubTokenBroker';

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const EXPIRES_AT = '2026-08-27T16:45:00Z';

function okResponse(body: unknown) {
  return { ok: true, status: 200, json: async () => body } as Response;
}

function errResponse(status: number, text: string) {
  return { ok: false, status, text: async () => text } as Response;
}

const REQ = {
  installationId: '555001',
  repoOwner: 'acme-corp',
  repoName: 'flagship-app',
};

describe('githubTokenBroker', () => {
  const ENV_KEYS = [
    'ADP_GH_TOKEN_BROKER_ENABLED',
    'ADP_AGENT_AUTHORITY_ENABLED',
    'ADP_RUN_CREDENTIAL_FILE',
    'ADP_WORKLOAD_TOKEN_FILE',
    'ADP_GATEWAY_ENDPOINT',
    'ADP_MESSAGE_ID',
    'VAULT_GATEWAY_URL',
    'VAULT_INTERNAL_API_KEY',
    'AWS_REGION',
    // Static credentials so defaultProvider() resolves deterministically and the
    // SigV4 assertions can be unconditional — same reasoning as
    // provenanceClient.test.ts. Without them, the signed path silently goes
    // unexercised in CI while the test still reports green.
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN',
    'AWS_PROFILE',
    'AWS_CONTAINER_CREDENTIALS_FULL_URI',
    'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
    'AWS_WEB_IDENTITY_TOKEN_FILE',
    'AWS_ROLE_ARN',
  ];

  function withFakeCredentials(): void {
    process.env.AWS_ACCESS_KEY_ID = 'AKIAIOSFODNN7EXAMPLE';
    process.env.AWS_SECRET_ACCESS_KEY = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY';
    process.env.AWS_REGION = 'us-east-1';
  }

  let saved: Record<string, string | undefined>;

  beforeEach(() => {
    mockFetch.mockReset();
    saved = {};
    for (const k of ENV_KEYS) {
      saved[k] = process.env[k];
      delete process.env[k];
    }
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    jest.spyOn(console, 'log').mockImplementation(() => {});
  });

  afterEach(() => {
    for (const k of ENV_KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
    jest.restoreAllMocks();
  });

  it('signs both current worker proofs on initial mint and refresh, without redirects', async () => {
    withFakeCredentials();
    const dir = mkdtempSync(join(tmpdir(), 'adp-broker-identity-'));
    process.env.ADP_AGENT_AUTHORITY_ENABLED = 'true';
    process.env.ADP_GATEWAY_ENDPOINT = 'https://api.example.test/dev';
    process.env.ADP_RUN_CREDENTIAL_FILE = join(dir, 'credential');
    process.env.ADP_WORKLOAD_TOKEN_FILE = join(dir, 'pod');
    writeFileSync(process.env.ADP_WORKLOAD_TOKEN_FILE, 'pod-proof');
    mockFetch.mockImplementation(async () => okResponse({ token: 'token', expires_at: EXPIRES_AT }));
    try {
      for (const epoch of [1, 2]) {
        writeFileSync(process.env.ADP_RUN_CREDENTIAL_FILE, `run-credential-${epoch}`);
        await fetchBrokeredToken(REQ);
        const init = mockFetch.mock.calls.at(-1)![1];
        expect(init.headers['X-Adp-Run-Credential']).toBe(`run-credential-${epoch}`);
        expect(init.headers['X-Adp-Workload-Token']).toBe('pod-proof');
        expect(init.headers.authorization).toContain('x-adp-run-credential;x-adp-workload-token');
        expect(init.redirect).toBe('error');
      }
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });

  it('refuses authority broker requests with missing identity or legacy transport', async () => {
    process.env.ADP_AGENT_AUTHORITY_ENABLED = 'true';
    process.env.ADP_GATEWAY_ENDPOINT = 'https://api.example.test/dev';
    await expect(fetchBrokeredToken(REQ)).rejects.toThrow('identity unavailable');
    delete process.env.ADP_GATEWAY_ENDPOINT;
    process.env.VAULT_GATEWAY_URL = 'https://legacy.example.test';
    process.env.VAULT_INTERNAL_API_KEY = 'legacy';
    await expect(fetchBrokeredToken(REQ)).rejects.toThrow('HTTPS and SigV4');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  describe('isBrokerEnabled', () => {
    it.each(['1', 'true', 'TRUE', 'yes'])('is true for %s', value => {
      expect(isBrokerEnabled({ ADP_GH_TOKEN_BROKER_ENABLED: value })).toBe(true);
    });

    it.each(['', '0', 'false', 'no', 'off', 'maybe'])('is false for "%s"', value => {
      expect(isBrokerEnabled({ ADP_GH_TOKEN_BROKER_ENABLED: value })).toBe(false);
    });

    it('is false when unset — default off, so today behavior is unchanged', () => {
      expect(isBrokerEnabled({})).toBe(false);
    });
  });

  describe('no-fallback contract', () => {
    it('throws when neither transport is configured', async () => {
      // The critical property: NOT a silent no-op. A no-op here would leave the
      // caller on its stale token and the run would die at the real expiry with
      // an unexplained 401 — which is how this class of bug hides.
      await expect(fetchBrokeredToken(REQ)).rejects.toThrow(/Gateway not configured/);
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it('throws when VAULT_GATEWAY_URL is set but the api key is missing', async () => {
      process.env.VAULT_GATEWAY_URL = 'https://gw.internal';
      await expect(fetchBrokeredToken(REQ)).rejects.toThrow(/Gateway not configured/);
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it('propagates a gateway 403 instead of swallowing it', async () => {
      process.env.VAULT_GATEWAY_URL = 'https://gw.internal';
      process.env.VAULT_INTERNAL_API_KEY = 'k';
      mockFetch.mockResolvedValue(
        errResponse(403, '{"detail":{"error":"installation_binding_mismatch"}}'),
      );

      await expect(fetchBrokeredToken(REQ)).rejects.toThrow(/403/);
    });
  });

  describe('request contract', () => {
    beforeEach(() => {
      process.env.VAULT_GATEWAY_URL = 'https://gw.internal';
      process.env.VAULT_INTERNAL_API_KEY = 'test-key';
      mockFetch.mockResolvedValue(okResponse({ token: 'ghs_x', expires_at: EXPIRES_AT }));
    });

    it('sends installation_id as a number and the repo split into owner/name', async () => {
      await fetchBrokeredToken(REQ);

      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      expect(body.installation_id).toBe(555001);
      expect(body.repo_owner).toBe('acme-corp');
      expect(body.repo_name).toBe('flagship-app');
    });

    it('sends NO tenant — the gateway derives it from the run record', async () => {
      await fetchBrokeredToken(REQ);

      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      // If the client asserted its own tenant, the ownership check would be
      // checking the caller's claim against itself and prove nothing.
      expect(body.tenant_id).toBeUndefined();
      expect(body.org_id).toBeUndefined();
    });

    it('defaults invocation_id from ADP_MESSAGE_ID', async () => {
      process.env.ADP_MESSAGE_ID = 'msg-from-env';
      await fetchBrokeredToken(REQ);

      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      expect(body.invocation_id).toBe('msg-from-env');
    });

    it('prefers an explicit invocation_id over the env default', async () => {
      process.env.ADP_MESSAGE_ID = 'msg-from-env';
      await fetchBrokeredToken({ ...REQ, invocationId: 'msg-explicit' });

      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      expect(body.invocation_id).toBe('msg-explicit');
    });

    it('omits invocation_id entirely when there is none to send', async () => {
      await fetchBrokeredToken(REQ);

      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      // Sending an empty string would read as "bound to nothing" rather than
      // "unknown"; the gateway rejects both, but only one is honest.
      expect('invocation_id' in body).toBe(false);
    });

    it('uses the shared-secret header on the non-SigV4 transport', async () => {
      await fetchBrokeredToken(REQ);

      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe('https://gw.internal/internal/v1/github-installation-token');
      expect(init.headers['X-Internal-Api-Key']).toBe('test-key');
    });
  });

  describe('SigV4 transport', () => {
    it('signs the request and targets the /internal proxy route, not /agent', async () => {
      withFakeCredentials();
      process.env.ADP_GATEWAY_ENDPOINT = 'https://api.execute-api.us-east-1.amazonaws.com/dev';
      mockFetch.mockResolvedValue(okResponse({ token: 'ghs_signed', expires_at: EXPIRES_AT }));

      const result = await fetchBrokeredToken(REQ);

      expect(result.token).toBe('ghs_signed');
      const [url, init] = mockFetch.mock.calls[0];
      // Issue #4343: this MUST be the API Gateway /internal/{proxy+} route,
      // which is wired to the internal-plane ALB. The /agent/{proxy+} route
      // integrates with the EDGE ALB, where #4010's edge-internal-deny patch
      // answers 403 "Not available from the edge" for /internal/* — and since
      // the broker has no local-mint fallback by design, that 403 killed every
      // agent run at bootstrap.
      expect(url).toBe(
        'https://api.execute-api.us-east-1.amazonaws.com/dev/internal/v1/github-installation-token',
      );
      expect(init.headers.authorization || init.headers.Authorization).toMatch(
        /AWS4-HMAC-SHA256.*execute-api/,
      );
      // Never the shared secret on this path.
      expect(init.headers['X-Internal-Api-Key']).toBeUndefined();
    });

    it('never routes the internal call through the edge-denied /agent prefix', async () => {
      // The regression guard for #4343 stated as a property rather than an exact
      // string: no /agent segment may appear anywhere in the signed URL.
      withFakeCredentials();
      process.env.ADP_GATEWAY_ENDPOINT = 'https://api.execute-api.us-east-1.amazonaws.com/dev';
      mockFetch.mockResolvedValue(okResponse({ token: 'ghs_signed', expires_at: EXPIRES_AT }));

      await fetchBrokeredToken(REQ);

      const url = mockFetch.mock.calls[0][0] as string;
      expect(url).not.toContain('/agent');
      expect(new URL(url).pathname).toBe('/dev/internal/v1/github-installation-token');
    });

    it('prefers SigV4 when both transports are configured', async () => {
      withFakeCredentials();
      process.env.ADP_GATEWAY_ENDPOINT = 'https://api.execute-api.us-east-1.amazonaws.com/dev';
      process.env.VAULT_GATEWAY_URL = 'https://gw.internal';
      process.env.VAULT_INTERNAL_API_KEY = 'test-key';
      mockFetch.mockResolvedValue(okResponse({ token: 'ghs_signed', expires_at: EXPIRES_AT }));

      await fetchBrokeredToken(REQ);

      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe(
        'https://api.execute-api.us-east-1.amazonaws.com/dev/internal/v1/github-installation-token',
      );
      expect(init.headers['X-Internal-Api-Key']).toBeUndefined();
    });

    it('tolerates a trailing slash on the endpoint', async () => {
      withFakeCredentials();
      process.env.ADP_GATEWAY_ENDPOINT = 'https://api.execute-api.us-east-1.amazonaws.com/dev/';
      mockFetch.mockResolvedValue(okResponse({ token: 'ghs_signed', expires_at: EXPIRES_AT }));

      await fetchBrokeredToken(REQ);

      // A doubled slash would make the path /dev//internal/v1/... which matches
      // no API Gateway route.
      expect(mockFetch.mock.calls[0][0]).toBe(
        'https://api.execute-api.us-east-1.amazonaws.com/dev/internal/v1/github-installation-token',
      );
    });
  });

  describe('expiry handling', () => {
    beforeEach(() => {
      process.env.VAULT_GATEWAY_URL = 'https://gw.internal';
      process.env.VAULT_INTERNAL_API_KEY = 'test-key';
    });

    it("returns GitHub's expiry, parsed — not a local now+1h guess", async () => {
      mockFetch.mockResolvedValue(okResponse({ token: 'ghs_x', expires_at: EXPIRES_AT }));

      const result = await fetchBrokeredToken(REQ);

      expect(result.expiresAt.toISOString()).toBe(new Date(EXPIRES_AT).toISOString());
    });

    it('throws when expires_at is missing', async () => {
      // A token whose refresh cannot be scheduled is unusable. Guessing the
      // expiry is precisely what makes runs die unpredictably at ~1h.
      mockFetch.mockResolvedValue(okResponse({ token: 'ghs_x' }));

      await expect(fetchBrokeredToken(REQ)).rejects.toThrow(/expires_at/);
    });

    it('throws when expires_at is unparseable', async () => {
      mockFetch.mockResolvedValue(okResponse({ token: 'ghs_x', expires_at: 'soon-ish' }));

      await expect(fetchBrokeredToken(REQ)).rejects.toThrow(/unparseable/);
    });

    it('throws when the token is missing', async () => {
      mockFetch.mockResolvedValue(okResponse({ expires_at: EXPIRES_AT }));

      await expect(fetchBrokeredToken(REQ)).rejects.toThrow(/token/);
    });
  });
});
