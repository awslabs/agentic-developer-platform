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
  if (preserved || process.env.ADP_AGENT_AUTHORITY_ENABLED?.toLowerCase() === 'true' || process.env.ADP_ARC_MODEL_POLICY_ENABLED?.toLowerCase() === 'true') {
    const roleArn = process.env[preserved ? 'ADP_WORKER_IRSA_ROLE_ARN' : 'AWS_ROLE_ARN'];
    const webIdentityTokenFile = process.env[preserved ? 'ADP_WORKER_IRSA_TOKEN_FILE' : 'AWS_WEB_IDENTITY_TOKEN_FILE'];
    if (!roleArn || !webIdentityTokenFile) throw new Error('Platform worker IRSA identity unavailable');
    const { fromTokenFile } = await import('@aws-sdk/credential-provider-web-identity');
    return fromTokenFile({
      roleArn,
      webIdentityTokenFile,
      roleSessionName: process.env[preserved ? 'ADP_WORKER_IRSA_SESSION_NAME' : 'AWS_ROLE_SESSION_NAME'],
      clientConfig: { region: workerAwsRegion() },
    });
  }
  const { defaultProvider } = await import('@aws-sdk/credential-provider-node');
  return defaultProvider();
}

export function workerAwsRegion(): string {
  return process.env.ADP_WORKER_AWS_REGION || process.env.AWS_REGION || 'us-east-1';
}

/** Lazy SDK provider for platform clients constructed before the run starts. */
export function workerAwsCredentials() {
  let provider: Awaited<ReturnType<typeof workerAwsCredentialProvider>> | undefined;
  return async () => {
    provider ??= await workerAwsCredentialProvider();
    return provider();
  };
}

/** Customer deployment regions must not change the gateway's signing scope. */
export function gatewaySigningRegion(endpoint: string): string {
  const match = new URL(endpoint).hostname.match(/^[a-z0-9-]+\.execute-api(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$/);
  return match?.[1] || workerAwsRegion();
}


/** Restore platform IRSA only for platform subprocesses such as beads S3 sync. */
export function workerAwsEnvironment(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  const preserved = 'ADP_WORKER_IRSA_ROLE_ARN' in env || 'ADP_WORKER_IRSA_TOKEN_FILE' in env;
  if (!preserved && env.ADP_AGENT_AUTHORITY_ENABLED?.toLowerCase() !== 'true') return env;
  const role = env[preserved ? 'ADP_WORKER_IRSA_ROLE_ARN' : 'AWS_ROLE_ARN'];
  const token = env[preserved ? 'ADP_WORKER_IRSA_TOKEN_FILE' : 'AWS_WEB_IDENTITY_TOKEN_FILE'];
  const session = env[preserved ? 'ADP_WORKER_IRSA_SESSION_NAME' : 'AWS_ROLE_SESSION_NAME'];
  if (!role || !token) throw new Error('Platform worker IRSA identity unavailable');
  for (const key of ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN',
    'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_ROLE_SESSION_NAME', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
    'AWS_CONTAINER_CREDENTIALS_FULL_URI']) delete env[key];
  env.AWS_ROLE_ARN = role;
  env.AWS_WEB_IDENTITY_TOKEN_FILE = token;
  if (session) env.AWS_ROLE_SESSION_NAME = session;
  env.AWS_CONFIG_FILE = '/dev/null';
  env.AWS_SHARED_CREDENTIALS_FILE = '/dev/null';
  env.AWS_REGION = workerAwsRegion();
  env.AWS_DEFAULT_REGION = env.AWS_REGION;
  return env;
}
