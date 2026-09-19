/** GitHub OIDC identifies the initiator; IRSA identifies the registered runner. */
import { createHash } from 'node:crypto';
import { SignatureV4 } from '@smithy/signature-v4';
import { Hash } from '@smithy/hash-node';
import type { workerAwsCredentialProvider } from './lib/runIdentity';
import { canonicalJson } from './invocability-probe/canonical-json';

export async function arcIdentity(nonce: string, region: string, credentials: Awaited<ReturnType<typeof workerAwsCredentialProvider>>, signal: AbortSignal) {
  if (!process.env.AWS_ROLE_ARN || !process.env.AWS_WEB_IDENTITY_TOKEN_FILE) throw new Error('ARC requires IRSA');
  const endpoint = new URL(process.env.ACTIONS_ID_TOKEN_REQUEST_URL || '');
  if (endpoint.protocol !== 'https:' || !/^[a-z0-9.-]+\.actions\.githubusercontent\.com$/.test(endpoint.hostname) || endpoint.username || endpoint.password) {
    throw new Error('GitHub OIDC endpoint unavailable');
  }
  const token = process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN;
  if (!token || /[\r\n]/.test(token)) throw new Error('GitHub OIDC credential unavailable');
  endpoint.searchParams.set('audience', 'adp-agent-model-policy');
  const response = await fetch(endpoint, { headers: { Authorization: `Bearer ${token}` }, signal, redirect: 'error' });
  if (!response.ok) throw new Error('GitHub OIDC refused');
  if (!response.body) throw new Error('GitHub OIDC unavailable');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > 20000) throw new Error('GitHub OIDC response too large');
      chunks.push(value);
    }
  } finally { await reader.cancel(); }
  const bytes = Buffer.concat(chunks).toString('utf8');
  const oidc = JSON.parse(bytes).value;
  if (typeof oidc !== 'string' || !oidc || oidc.length > 16384) throw new Error('GitHub OIDC unavailable');
  const body = JSON.stringify({ nonce, model_policy_contract: 1, github_oidc_token: oidc });
  const canonical = canonicalJson(JSON.parse(body)).replace(/[\u007f-\uffff]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
  const frozen = await credentials();
  if (!frozen.sessionToken) throw new Error('ARC requires temporary credentials');
  const signer = new SignatureV4({ credentials: frozen, region, service: 'sts', applyChecksum: false, sha256: Hash.bind(null, 'sha256') });
  const proof = await signer.sign({ method: 'POST', protocol: 'https:', hostname: `sts.${region}.amazonaws.com`, path: '/',
    headers: { host: `sts.${region}.amazonaws.com`, 'content-type': 'application/x-www-form-urlencoded',
      'x-adp-work-invocation': createHash('sha256').update(canonical).digest('hex') },
    body: 'Action=GetCallerIdentity&Version=2011-06-15' });
  const headers: Record<string, string> = {};
  for (const [key, value] of Object.entries(proof.headers)) {
    // host is reconstructed by STS; the server rejects any extra proof headers.
    if (key.toLowerCase() !== 'host' && key.toLowerCase() !== 'x-amz-content-sha256') headers[key.toLowerCase()] = value;
  }
  // Do not sign x-amz-content-sha256 if it is removed from the forwarded proof.
  if (proof.headers.authorization.includes('x-amz-content-sha256;')) throw new Error('Unsupported STS proof');
  return { body, headers: { 'X-Adp-Producer-Proof': Buffer.from(JSON.stringify(headers)).toString('base64') } };
}
