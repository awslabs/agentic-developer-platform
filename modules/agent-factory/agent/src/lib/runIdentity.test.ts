import { workerAwsCredentialProvider, gatewaySigningRegion, workerAwsCredentials, workerAwsRegion, workerAwsEnvironment } from './runIdentity';

const mockIrsa = jest.fn();
const mockDefault = jest.fn();
const mockFromTokenFile = jest.fn((_options: unknown) => mockIrsa);
jest.mock('@aws-sdk/credential-provider-web-identity', () => ({ fromTokenFile: (options: unknown) => mockFromTokenFile(options) }));
jest.mock('@aws-sdk/credential-provider-node', () => ({ defaultProvider: () => mockDefault }));

const saved = { ...process.env };
beforeEach(() => {
  for (const key of Object.keys(process.env)) {
    if (key.startsWith('AWS_') || key.startsWith('ADP_WORKER_')) delete process.env[key];
  }
  process.env.ADP_AGENT_AUTHORITY_ENABLED = 'true';
  jest.clearAllMocks();
});
afterEach(() => { process.env = { ...saved }; });

test('authority traffic uses explicit IRSA while task tools hold customer keys', async () => {
  process.env.AWS_ROLE_ARN = 'arn:aws:iam::123456789012:role/worker';
  process.env.AWS_WEB_IDENTITY_TOKEN_FILE = '/projected/token';
  process.env.AWS_ACCESS_KEY_ID = 'customer-task-key';
  expect(await workerAwsCredentialProvider()).toBe(mockIrsa);
  expect(mockFromTokenFile).toHaveBeenCalledWith(expect.objectContaining({
    roleArn: process.env.AWS_ROLE_ARN, webIdentityTokenFile: '/projected/token',
  }));
});

test('nested task role variables cannot replace preserved platform identity', async () => {
  process.env.ADP_WORKER_IRSA_ROLE_ARN = 'arn:aws:iam::123456789012:role/worker';
  process.env.ADP_WORKER_IRSA_TOKEN_FILE = '/projected/worker';
  process.env.ADP_WORKER_AWS_REGION = 'us-east-1';
  process.env.AWS_ROLE_ARN = 'arn:aws:iam::222222222222:role/customer-next-hop';
  process.env.AWS_WEB_IDENTITY_TOKEN_FILE = '/customer/token';
  process.env.AWS_REGION = 'us-west-2';
  const before = { ...process.env };
  expect(await workerAwsCredentialProvider()).toBe(mockIrsa);
  expect(mockFromTokenFile).toHaveBeenCalledWith(expect.objectContaining({
    roleArn: process.env.ADP_WORKER_IRSA_ROLE_ARN, webIdentityTokenFile: '/projected/worker',
    clientConfig: { region: 'us-east-1' },
  }));
  expect(process.env).toEqual(before);
});

test.each(['both', 'role', 'token'])('missing protected identity (%s) cannot fall back to task keys', async missing => {
  process.env.AWS_ACCESS_KEY_ID = 'customer-task-key';
  if (missing === 'role') process.env.ADP_WORKER_IRSA_TOKEN_FILE = '/projected/token';
  if (missing === 'token') process.env.ADP_WORKER_IRSA_ROLE_ARN = 'worker';
  await expect(workerAwsCredentialProvider()).rejects.toThrow('IRSA identity unavailable');
  expect(mockFromTokenFile).not.toHaveBeenCalled();
});

test('legacy caller without preserved identity retains the default provider', async () => {
  process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
  expect(await workerAwsCredentialProvider()).toBe(mockDefault);
});

test.each([
  ['https://abc.execute-api.eu-west-1.amazonaws.com/dev', 'eu-west-1'],
  ['https://abc.execute-api.cn-north-1.amazonaws.com.cn/dev', 'cn-north-1'],
  ['https://custom-gateway.test/dev', 'us-east-1'],
  ['https://abc.execute-api.eu-west-1.amazonaws.com.invalid/dev', 'us-east-1'],
])('gateway region for %s stays independent of the task', (endpoint, expected) => {
  process.env.ADP_WORKER_AWS_REGION = 'us-east-1';
  process.env.AWS_REGION = 'us-west-2';
  expect(gatewaySigningRegion(endpoint)).toBe(expected);
});


test('platform SDK provider is lazy, refreshes credentials, and retains the platform region', async () => {
  process.env.ADP_WORKER_IRSA_ROLE_ARN = 'worker';
  process.env.ADP_WORKER_IRSA_TOKEN_FILE = '/projected/worker';
  process.env.ADP_WORKER_AWS_REGION = 'eu-west-1';
  process.env.AWS_REGION = 'us-west-2';
  const provider = workerAwsCredentials();
  expect(mockFromTokenFile).not.toHaveBeenCalled();
  mockIrsa.mockResolvedValueOnce({ accessKeyId: 'first' }).mockResolvedValueOnce({ accessKeyId: 'refreshed' });
  expect(await provider()).toEqual({ accessKeyId: 'first' });
  expect(await provider()).toEqual({ accessKeyId: 'refreshed' });
  expect(mockFromTokenFile).toHaveBeenCalledTimes(1);
  expect(workerAwsRegion()).toBe('eu-west-1');
  expect(process.env.AWS_REGION).toBe('us-west-2');
});


test.each([false, true])('platform subprocess identity (%s) ignores customer config and credentials', preserved => {
  process.env[preserved ? 'ADP_WORKER_IRSA_ROLE_ARN' : 'AWS_ROLE_ARN'] = 'platform-role';
  process.env[preserved ? 'ADP_WORKER_IRSA_TOKEN_FILE' : 'AWS_WEB_IDENTITY_TOKEN_FILE'] = '/projected/worker';
  process.env[preserved ? 'ADP_WORKER_IRSA_SESSION_NAME' : 'AWS_ROLE_SESSION_NAME'] = 'platform-session';
  process.env.ADP_WORKER_AWS_REGION = 'us-east-1';
  process.env.AWS_REGION = 'us-west-2';
  process.env.AWS_ACCESS_KEY_ID = 'customer-key';
  process.env.AWS_SECRET_ACCESS_KEY = 'customer-secret';
  process.env.AWS_PROFILE = 'customer';
  process.env.AWS_CONFIG_FILE = '/task/config';
  const before = { ...process.env };
  const env = workerAwsEnvironment();
  expect(env.AWS_ROLE_ARN).toBe('platform-role');
  expect(env.AWS_WEB_IDENTITY_TOKEN_FILE).toBe('/projected/worker');
  expect(env.AWS_ROLE_SESSION_NAME).toBe('platform-session');
  expect(env.AWS_CONFIG_FILE).toBe('/dev/null');
  expect(env.AWS_SHARED_CREDENTIALS_FILE).toBe('/dev/null');
  expect(env.AWS_ACCESS_KEY_ID).toBeUndefined();
  expect(env.AWS_SECRET_ACCESS_KEY).toBeUndefined();
  expect(env.AWS_PROFILE).toBeUndefined();
  expect(env.AWS_REGION).toBe('us-east-1');
  expect(process.env).toEqual(before);
});

test('platform subprocess cannot silently fall back to customer keys', () => {
  process.env.AWS_ACCESS_KEY_ID = 'customer-key';
  expect(() => workerAwsEnvironment()).toThrow('IRSA identity unavailable');
});

test('legacy subprocess preserves the complete original environment', () => {
  process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
  expect(workerAwsEnvironment()).toEqual(process.env);
});
