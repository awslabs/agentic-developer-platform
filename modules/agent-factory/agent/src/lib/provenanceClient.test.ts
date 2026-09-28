/**
 * Tests for provenanceClient.ts.
 *
 * Issue #4029: this client had no tests at all, which is why it carried the same
 * three payload/response defects as its Python twin plus a fourth (no SigV4 path,
 * so it silently no-op'd in IRSA-only environments).
 *
 * The payload assertions consume the SAME golden fixture as the Python worker and
 * gateway suites — contracts/provenance/v1/create-provenance-request.golden.json —
 * so all three producers are pinned to one artifact.
 */

import * as fs from 'fs';
import * as path from 'path';
import { buildProvenanceBody, postProvenance, ProvenancePayload } from './provenanceClient';

const GOLDEN_PATH = path.resolve(
  __dirname,
  '../../../../../contracts/provenance/v1/create-provenance-request.golden.json',
);

const GOLDEN = JSON.parse(fs.readFileSync(GOLDEN_PATH, 'utf-8'));
const GOLDEN_REQUEST = GOLDEN.request as Record<string, unknown>;
const GOLDEN_RESPONSE = GOLDEN.response as Record<string, string>;

/** The golden builder_inputs, expressed in this client's camelCase interface. */
function goldenPayload(): ProvenancePayload {
  const i = GOLDEN.builder_inputs;
  return {
    actorUserId: i.actor_user_id,
    triggeredBy: i.triggered_by,
    rootHumanId: i.root_human_id,
    isHumanRooted: i.is_human_rooted,
    actionKind: i.action_kind,
    sourceEvent: i.source_event,
    correlationId: i.correlation_id,
    orgId: i.org_id,
    parentInvocationId: i.parent_invocation_id,
  };
}

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

function okResponse(body: unknown) {
  return { ok: true, status: 201, json: async () => body } as Response;
}

describe('provenanceClient', () => {
  const ENV_KEYS = [
    'ADP_GATEWAY_ENDPOINT',
    'VAULT_GATEWAY_URL',
    'VAULT_INTERNAL_API_KEY',
    'AWS_REGION',
    // Static credentials so defaultProvider() resolves deterministically and the
    // SigV4 assertions below can be unconditional. Without these, credential
    // resolution depends on the ambient environment (present on a dev laptop,
    // absent in CI), and an `if (calls.length)` guard would let the signed path
    // go unexercised while the test still reported green.
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN',
    'AWS_PROFILE',
    'AWS_CONTAINER_CREDENTIALS_FULL_URI',
    'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
    'AWS_WEB_IDENTITY_TOKEN_FILE',
    'AWS_ROLE_ARN',
  ];

  /** Fake static creds — never used against a real endpoint (fetch is mocked). */
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
  });

  afterEach(() => {
    for (const k of ENV_KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
    jest.restoreAllMocks();
  });

  describe('payload contract (shared golden fixture)', () => {
    it('builds exactly the golden request body', () => {
      // The gateway suite validates this same object against
      // CreateProvenanceRequest, so drift on either side fails a test.
      expect(buildProvenanceBody(goldenPayload())).toEqual(GOLDEN_REQUEST);
    });

    it('sends source_event as an object, not a string', () => {
      const body = buildProvenanceBody(goldenPayload());
      expect(typeof body.source_event).toBe('object');
    });

    it('sends a non-null org_id', () => {
      const body = buildProvenanceBody(goldenPayload());
      expect(typeof body.org_id).toBe('string');
      expect(body.org_id).toBeTruthy();
    });

    it('has exactly the gateway schema field names', () => {
      expect(Object.keys(buildProvenanceBody(goldenPayload())).sort()).toEqual(
        Object.keys(GOLDEN_REQUEST).sort(),
      );
    });

    it('defaults parent_invocation_id to null when omitted', () => {
      const { parentInvocationId: _drop, ...rest } = goldenPayload();
      expect(buildProvenanceBody(rest as ProvenancePayload).parent_invocation_id).toBeNull();
    });
  });

  describe('transport selection', () => {
    it('returns null when nothing is configured', async () => {
      expect(await postProvenance(goldenPayload())).toBeNull();
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it('returns null when the legacy config is incomplete', async () => {
      process.env.VAULT_GATEWAY_URL = 'http://gateway:8080';
      // no API key
      expect(await postProvenance(goldenPayload())).toBeNull();
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it('posts with the shared-secret header on the legacy path', async () => {
      process.env.VAULT_GATEWAY_URL = 'http://gateway:8080';
      process.env.VAULT_INTERNAL_API_KEY = 'legacy-key';
      mockFetch.mockResolvedValue(okResponse(GOLDEN_RESPONSE));

      const result = await postProvenance(goldenPayload());

      expect(result).toBe(GOLDEN_RESPONSE.id);
      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe('http://gateway:8080/internal/v1/provenance');
      expect((init.headers as Record<string, string>)['X-Internal-Api-Key']).toBe('legacy-key');
      expect(JSON.parse(init.body as string)).toEqual(GOLDEN_REQUEST);
    });

    it('signs with SigV4 when only ADP_GATEWAY_ENDPOINT is set', async () => {
      // Issue #4029 fourth defect: this used to return null without trying,
      // so IRSA-only environments never wrote provenance from the Node runtime.
      process.env.ADP_GATEWAY_ENDPOINT = 'https://api-gw.example.com';
      withFakeCredentials();
      mockFetch.mockResolvedValue(okResponse(GOLDEN_RESPONSE));

      const result = await postProvenance(goldenPayload());

      // Unconditional: with credentials available, the signed request MUST go out.
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toBe('https://api-gw.example.com/agent/internal/v1/provenance');
      expect(result).toBe(GOLDEN_RESPONSE.id);

      const headers = init.headers as Record<string, string>;
      // A real AWS4-HMAC-SHA256 Authorization header, not just "didn't crash".
      expect(headers.authorization ?? headers.Authorization).toMatch(
        /^AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE\/\d{8}\/us-east-1\/execute-api\/aws4_request/,
      );
      expect(headers['x-amz-date'] ?? headers['X-Amz-Date']).toBeDefined();
      // The shared secret must never leak onto the API Gateway host.
      expect(headers['X-Internal-Api-Key']).toBeUndefined();
      expect(JSON.parse(init.body as string)).toEqual(GOLDEN_REQUEST);
    });

    it('prefers SigV4 over the legacy shared secret when both are configured', async () => {
      process.env.ADP_GATEWAY_ENDPOINT = 'https://api-gw.example.com';
      process.env.VAULT_GATEWAY_URL = 'http://gateway:8080';
      process.env.VAULT_INTERNAL_API_KEY = 'legacy-key';
      withFakeCredentials();
      mockFetch.mockResolvedValue(okResponse(GOLDEN_RESPONSE));

      await postProvenance(goldenPayload());

      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [url, init] = mockFetch.mock.calls[0];
      expect(url).toContain('api-gw.example.com');
      const headers = init.headers as Record<string, string>;
      expect(headers.authorization ?? headers.Authorization).toContain('AWS4-HMAC-SHA256');
      expect(headers['X-Internal-Api-Key']).toBeUndefined();
    });

    it('fail-softs to null when SigV4 credentials cannot be resolved', async () => {
      // The counterpart to the two tests above: configured for SigV4 but with no
      // resolvable credentials, it must return null rather than throw into the
      // caller's action path. beforeEach already cleared every credential source.
      process.env.ADP_GATEWAY_ENDPOINT = 'https://api-gw.example.com';
      process.env.AWS_REGION = 'us-east-1';

      await expect(postProvenance(goldenPayload())).resolves.toBeNull();
      expect(mockFetch).not.toHaveBeenCalled();
    });
  });

  describe('response parsing', () => {
    beforeEach(() => {
      process.env.VAULT_GATEWAY_URL = 'http://gateway:8080';
      process.env.VAULT_INTERNAL_API_KEY = 'key';
    });

    it("reads the 'id' key the gateway actually returns", async () => {
      mockFetch.mockResolvedValue(okResponse({ id: 'prov-real-1', created_at: 'now' }));
      expect(await postProvenance(goldenPayload())).toBe('prov-real-1');
    });

    it("does not accept the fabricated 'provenance_id' key", async () => {
      // Pins the latent third defect shut.
      mockFetch.mockResolvedValue(okResponse({ provenance_id: 'prov-123' }));
      expect(await postProvenance(goldenPayload())).toBeNull();
    });

    it('returns null on a non-2xx response', async () => {
      mockFetch.mockResolvedValue({ ok: false, status: 422, json: async () => ({}) } as Response);
      expect(await postProvenance(goldenPayload())).toBeNull();
    });

    it('returns null and never throws when the request rejects', async () => {
      mockFetch.mockRejectedValue(new Error('ECONNREFUSED'));
      await expect(postProvenance(goldenPayload())).resolves.toBeNull();
    });
  });
});
