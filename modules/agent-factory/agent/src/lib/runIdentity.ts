/** Read the supervisor's current credential and projected pod proof per request. */
import { constants, openSync, fstatSync, readSync, closeSync } from 'node:fs';

export function readIdentityToken(path: string | undefined): string {
  if (!path) throw new Error('identity unavailable');
  const fd = openSync(path, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    if (!fstatSync(fd).isFile()) throw new Error('identity unavailable');
    const bytes = Buffer.alloc(16387);
    const count = readSync(fd, bytes, 0, bytes.length, null);
    const token = bytes.subarray(0, count).toString('utf8').replace(/\r?\n$/, '');
    if (!token || token.length > 16384 || !/^[\x21-\x7e]+$/.test(token)) throw new Error('identity unavailable');
    return token;
  } finally { closeSync(fd); }
}

export function workerIdentityHeaders(): Record<string, string> {
  return {
    'X-Adp-Run-Credential': readIdentityToken(process.env.ADP_RUN_CREDENTIAL_FILE),
    'X-Adp-Workload-Token': readIdentityToken(process.env.ADP_WORKLOAD_TOKEN_FILE),
  };
}


/** Platform calls must not switch to credentials loaded for an Operations task. */
export async function workerAwsCredentialProvider() {
  if (process.env.ADP_AGENT_AUTHORITY_ENABLED === 'true' && process.env.AWS_ROLE_ARN) {
    const { fromTokenFile } = await import('@aws-sdk/credential-provider-web-identity');
    return fromTokenFile();
  }
  const { defaultProvider } = await import('@aws-sdk/credential-provider-node');
  return defaultProvider();
}
