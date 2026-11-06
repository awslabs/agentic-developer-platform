import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  devOpsSetupDraft, expiryValid, grantOwnerConsent, listOwnerGrants, previewOwnerConsent, revokeOwnerConsent,
  sameConsentScope, type ConsentReceipt, type OwnerGrant,
} from '@/services/l5OwnerConsent';

afterEach(() => vi.restoreAllMocks());
describe('owner consent transport', () => {
  it('encodes the route and sends the explicit DevOps setup selection', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { headers: { ETag: '"config-hash"' } }));
    await previewOwnerConsent('product/a', 'project/b', 'devops', { for_control: true, control_action: 'devops_setup' });
    const [url, request] = fetch.mock.calls[0];
    expect(url).toContain('/products/product%2Fa/projects/project%2Fb/services/devops/delegation-scope?for_control=true&control_action=devops_setup');
    expect(url).not.toContain('binding_id');
    expect(request?.method).toBe('GET');
  });
  it('replays the exact grant body, expiry, ETag and original request key', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('{}', { headers: { ETag: '"1"' } }));
    const receipt = { key: 'original_request_1234', etag: '"configuration-original"',
      body: { scope_digest: 'a'.repeat(64), binding_id: 'binding-original', for_control: true, expires_at: '2030-01-01T01:00:00Z' } } as ConsentReceipt;
    await grantOwnerConsent('product', 'project', 'security', receipt);
    await grantOwnerConsent('product', 'project', 'security', receipt);
    expect(fetch.mock.calls[1]).toEqual(fetch.mock.calls[0]);
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: 'POST', body: JSON.stringify(receipt.body), headers: expect.objectContaining({
      'Idempotency-Key': receipt.key, 'If-Match': receipt.etag,
    }) });
  });
  it('uses the grant revision for revocation and rejects a missing read ETag', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response('{}', { headers: { ETag: '"8"' } }))
      .mockResolvedValueOnce(new Response('{}'));
    await revokeOwnerConsent('product', 'project', 'finops', { delegation_id: 'grant/a', revision: 7 } as OwnerGrant);
    expect(fetch.mock.calls[0][0]).toContain('/delegations/grant%2Fa');
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: 'DELETE', headers: expect.objectContaining({ 'If-Match': '"7"' }) });
    await expect(previewOwnerConsent('product', 'project', 'finops', { for_control: false })).rejects.toThrow('revision token');
  });
  it('only reads when listing existing grants', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{"items":[]}'));
    await listOwnerGrants('product', 'project', 'transform');
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1]?.method).toBe('GET');
  });
});

describe('bounded consent choices', () => {
  const now = Date.parse('2030-01-01T00:00:00Z');
  it('requires an explicit UTC expiry strictly beyond one minute and within 24 hours', () => {
    expect(expiryValid('', now)).toBe(false);
    expect(expiryValid('2030-01-01T01:00:00', now)).toBe(false);
    expect(expiryValid('2030-01-01T00:01:00Z', now)).toBe(false);
    expect(expiryValid('2030-01-01T00:01:01Z', now)).toBe(true);
    expect(expiryValid('2030-01-02T00:00:00Z', now)).toBe(true);
    expect(expiryValid('2030-01-02T00:00:01Z', now)).toBe(false);
  });
  it('compares complete scope independent of object key order without ignoring changes', () => {
    expect(sameConsentScope({ a: 1, b: { c: 2 } }, { b: { c: 2 }, a: 1 })).toBe(true);
    expect(sameConsentScope({ a: 1 }, { a: 1, branch_schedules: [] })).toBe(false);
  });
  it('admits only the exact disabled DevOps setup selection', () => {
    const draft = { enabled: false, capabilities: ['investigation'], triggers: [], devops: { space_mode: 'create' } };
    expect(devOpsSetupDraft(draft)).toBe(true);
    for (const mutation of [{ enabled: true }, { capabilities: ['investigation', 'prevention'] }, { triggers: [{}] },
      { devops: { space_mode: 'adopt' } }, { devops: { space_mode: 'create', prevention_enabled: true } },
      { devops: { space_mode: 'create', browser_launch_policy: 'scoped_session' } },
      { devops: { space_mode: 'create', permitted_native_actions: ['start'] } }]) {
      expect(devOpsSetupDraft({ ...draft, ...mutation })).toBe(false);
    }
  });
});
