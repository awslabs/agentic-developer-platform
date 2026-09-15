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
  const preserved = 'ADP_WORKER_IRSA_ROLE_ARN' in process.env || 'ADP_WORKER_IRSA_TOKEN_FILE' in process.env;
  if (preserved || process.env.ADP_AGENT_AUTHORITY_ENABLED?.toLowerCase() === 'true') {
    const roleArn = process.env[preserved ? 'ADP_WORKER_IRSA_ROLE_ARN' : 'AWS_ROLE_ARN'];
    const webIdentityTokenFile = process.env[preserved ? 'ADP_WORKER_IRSA_TOKEN_FILE' : 'AWS_WEB_IDENTITY_TOKEN_FILE'];
    if (!roleArn || !webIdentityTokenFile) throw new Error('Platform worker IRSA identity unavailable');
    const { fromTokenFile } = await import('@aws-sdk/credential-provider-web-identity');
    return fromTokenFile({
      roleArn,
      webIdentityTokenFile,
      roleSessionName: process.env[preserved ? 'ADP_WORKER_IRSA_SESSION_NAME' : 'AWS_ROLE_SESSION_NAME'],
      clientConfig: { region: process.env.ADP_WORKER_AWS_REGION || process.env.AWS_REGION || 'us-east-1' },
    });
  }
  const { defaultProvider } = await import('@aws-sdk/credential-provider-node');
  return defaultProvider();
}

/** Customer deployment regions must not change the gateway's signing scope. */
export function gatewaySigningRegion(endpoint: string): string {
  const match = new URL(endpoint).hostname.match(/^[a-z0-9-]+\.execute-api(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$/);
  return match?.[1] || process.env.ADP_WORKER_AWS_REGION || process.env.AWS_REGION || 'us-east-1';
}
