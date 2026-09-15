import { workerAwsCredentialProvider } from './runIdentity';

const mockIrsa = jest.fn();
const mockDefault = jest.fn();
jest.mock('@aws-sdk/credential-provider-web-identity', () => ({ fromTokenFile: () => mockIrsa }));
jest.mock('@aws-sdk/credential-provider-node', () => ({ defaultProvider: () => mockDefault }));

test('authority traffic retains IRSA when task tools load customer AWS credentials', async () => {
  const saved = { ...process.env };
  try {
    process.env.ADP_AGENT_AUTHORITY_ENABLED = 'true';
    process.env.AWS_ROLE_ARN = 'arn:aws:iam::123456789012:role/authority-worker';
    process.env.AWS_ACCESS_KEY_ID = 'customer-task-key';
    expect(await workerAwsCredentialProvider()).toBe(mockIrsa);
    process.env.ADP_AGENT_AUTHORITY_ENABLED = 'false';
    expect(await workerAwsCredentialProvider()).toBe(mockDefault);
  } finally { process.env = saved; }
});
