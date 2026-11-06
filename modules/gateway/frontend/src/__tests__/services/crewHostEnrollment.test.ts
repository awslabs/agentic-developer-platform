import { webcrypto } from 'node:crypto';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { enrollCrewHost } from '@/services/crewHostEnrollment';
import { apiClient } from '@/services/api';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));

const desktop = 'box-current';
const challenge = 'the-current-owner-only-challenge-1234567890';
const device = '12345678-1234-1234-1234-123456789012';
let publicKey: string;
let digest: string;
let privateKey: CryptoKey;

beforeAll(async () => {
  const pair = await webcrypto.subtle.generateKey(
    { name: 'RSA-OAEP', modulusLength: 3072, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' },
    true, ['encrypt', 'decrypt'],
  );
  privateKey = pair.privateKey as CryptoKey;
  const der = Buffer.from(await webcrypto.subtle.exportKey('spki', pair.publicKey));
  const lines = der.toString('base64').match(/.{1,64}/g) ?? [];
  publicKey = `-----BEGIN PUBLIC KEY-----\n${lines.join('\n')}\n-----END PUBLIC KEY-----\n`;
  digest = Buffer.from(await webcrypto.subtle.digest('SHA-256', Buffer.from(publicKey))).toString('hex');
});

beforeEach(() => {
  vi.mocked(apiClient.get).mockReset();
  vi.mocked(apiClient.post).mockReset();
  vi.stubGlobal('crypto', webcrypto);
  vi.mocked(apiClient.get).mockImplementation(async () => ({
    public_key_b64: Buffer.from(publicKey).toString('base64'), public_key_digest: digest, generation: 3,
  }));
  vi.mocked(apiClient.post).mockResolvedValueOnce({
    device_id: device, challenge, expires_at: new Date(Date.now() + 60_000).toISOString(),
  }).mockResolvedValueOnce({ status: 'pending' });
});

afterEach(() => vi.unstubAllGlobals());

describe('explicit Crew host enrollment', () => {
  it('sends only a digest and RSA-OAEP ciphertext for the selected box', async () => {
    await enrollCrewHost(desktop);
    expect(apiClient.get).toHaveBeenCalledWith(`/cloud-desktops/${desktop}/host-enrollment-key`, undefined);
    expect(apiClient.post).toHaveBeenCalledTimes(2);
    expect(apiClient.post).toHaveBeenNthCalledWith(1, `/cloud-desktops/${desktop}/host-enrollment`,
      { public_key_digest: digest }, undefined);
    const [url, body] = vi.mocked(apiClient.post).mock.calls[1];
    expect(url).toBe(`/cloud-desktops/${desktop}/host-enrollment/${device}/sealed`);
    expect(body).toEqual({ ciphertext_b64: expect.stringMatching(/^[A-Za-z0-9+/]{512}$/) });
    expect(JSON.stringify(vi.mocked(apiClient.post).mock.calls)).not.toContain(challenge);
    const plaintext = await webcrypto.subtle.decrypt({ name: 'RSA-OAEP' }, privateKey,
      Buffer.from((body as { ciphertext_b64: string }).ciphertext_b64, 'base64'));
    expect(new TextDecoder().decode(plaintext)).toBe(challenge);
  });

  it('refuses a substituted key digest before requesting a challenge', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce({
      public_key_b64: Buffer.from(publicKey).toString('base64'), public_key_digest: '0'.repeat(64), generation: 3,
    });
    await expect(enrollCrewHost(desktop)).rejects.toThrow('Crew host key changed');
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('refuses invalid and expired approvals without sending a ciphertext', async () => {
    vi.mocked(apiClient.post).mockReset().mockResolvedValueOnce({
      device_id: device, challenge, expires_at: new Date(Date.now() - 1_000).toISOString(),
    });
    await expect(enrollCrewHost(desktop)).rejects.toThrow('Invalid Crew enrollment');
    expect(apiClient.post).toHaveBeenCalledTimes(1);
  });

  it('propagates a changed host denial rather than treating encryption as enrollment', async () => {
    vi.mocked(apiClient.post).mockReset().mockResolvedValueOnce({
      device_id: device, challenge, expires_at: new Date(Date.now() + 60_000).toISOString(),
    }).mockRejectedValueOnce({ status: 409 });
    await expect(enrollCrewHost(desktop)).rejects.toEqual({ status: 409 });
    expect(apiClient.post).toHaveBeenCalledTimes(2);
  });

  it('rejects box path injection without any network calls', async () => {
    await expect(enrollCrewHost('../foreign')).rejects.toThrow('Invalid Crew box');
    expect(apiClient.get).not.toHaveBeenCalled();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('refuses a browser without WebCrypto instead of sending plaintext', async () => {
    vi.stubGlobal('crypto', {});
    await expect(enrollCrewHost(desktop)).rejects.toThrow('Secure browser encryption is required');
    expect(apiClient.get).not.toHaveBeenCalled();
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});
