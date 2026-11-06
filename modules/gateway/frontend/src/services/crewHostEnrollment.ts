import { apiClient } from './api';

type HostKey = { public_key_b64: string; public_key_digest: string; generation: number };
type HostApproval = { device_id: string; challenge: string; expires_at: string };

function decodeBase64(encoded: string): Uint8Array<ArrayBuffer> {
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
    throw new Error('Invalid Crew host key');
  }
  return Uint8Array.from(atob(encoded), value => value.charCodeAt(0));
}

function encodeBase64(bytes: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)));
}

function verifyKey(response: HostKey): { key: Uint8Array<ArrayBuffer>; digest: string; generation: number } {
  if (!response || !Number.isSafeInteger(response.generation) || response.generation < 1 ||
      !/^[0-9a-f]{64}$/.test(response.public_key_digest)) throw new Error('Invalid Crew host key');
  const key = decodeBase64(response.public_key_b64);
  if (key.length < 512 || key.length > 2048) throw new Error('Invalid Crew host key');
  return { key, digest: response.public_key_digest, generation: response.generation };
}

export async function enrollCrewHost(desktopId: string, signal?: AbortSignal): Promise<void> {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(desktopId)) throw new Error('Invalid Crew box');
  if (!globalThis.crypto?.subtle) throw new Error('Secure browser encryption is required');
  const endpoint = `/cloud-desktops/${encodeURIComponent(desktopId)}/host-enrollment`;
  const observed = verifyKey(await apiClient.get<HostKey>(`${endpoint}-key`, signal));
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', observed.key));
  if (Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('') !== observed.digest) {
    throw new Error('Crew host key changed');
  }
  const pem = new TextDecoder('ascii', { fatal: true }).decode(observed.key);
  const match = /^-----BEGIN PUBLIC KEY-----\n([A-Za-z0-9+/=\n]+)-----END PUBLIC KEY-----\n$/.exec(pem);
  if (!match) throw new Error('Invalid Crew host key');
  const encoded = match[1].replace(/\n/g, '');
  const publicKey = await crypto.subtle.importKey(
    'spki', decodeBase64(encoded), { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt'],
  );
  if (publicKey.algorithm.name !== 'RSA-OAEP' ||
      !('modulusLength' in publicKey.algorithm) || publicKey.algorithm.modulusLength !== 3072) {
    throw new Error('Invalid Crew host key');
  }
  const approval = await apiClient.post<HostApproval>(endpoint, { public_key_digest: observed.digest }, signal);
  if (!approval || !/^[A-Za-z0-9_-]{32,128}$/.test(approval.challenge) ||
      !/^[A-Za-z0-9-]{32,64}$/.test(approval.device_id) ||
      !Number.isFinite(Date.parse(approval.expires_at)) || Date.parse(approval.expires_at) <= Date.now()) {
    throw new Error('Invalid Crew enrollment');
  }
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'RSA-OAEP' }, publicKey, new TextEncoder().encode(approval.challenge),
  );
  if (ciphertext.byteLength !== 384 || signal?.aborted || Date.parse(approval.expires_at) <= Date.now()) {
    throw new Error('Crew enrollment expired');
  }
  await apiClient.post(`${endpoint}/${encodeURIComponent(approval.device_id)}/sealed`,
    { ciphertext_b64: encodeBase64(ciphertext) }, signal);
}
