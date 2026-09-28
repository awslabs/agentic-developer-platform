/**
 * Behavioural tests for saveToS3Fallback()'s bucket handling — issue #4184.
 *
 * These assert the OUTCOME (whether a PutObject was attempted at all, and
 * against which bucket), not the source text. They fail on pre-fix code, where
 * an unset AGENT_FALLBACK_BUCKET produced a PutObject against the hardcoded
 * `adp-agent-state` — a bucket in a foreign AWS account that the agent's IAM
 * policy does not permit, so the write was silently AccessDenied.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

const mockSend = jest.fn();
const mockPutObjectCommand = jest.fn((input: any) => ({ input }));

jest.mock('@aws-sdk/client-s3', () => ({
  __esModule: true,
  S3Client: jest.fn(() => ({ send: mockSend })),
  PutObjectCommand: mockPutObjectCommand,
}));

import { saveToS3Fallback } from './ghPost';

const ORIGINAL_ENV = process.env;

describe('saveToS3Fallback bucket resolution', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSend.mockResolvedValue({});
    process.env = { ...ORIGINAL_ENV, REPO_OWNER: 'aws-e', REPO_NAME: 'adp' };
    delete process.env.AGENT_FALLBACK_BUCKET;
  });

  afterAll(() => {
    process.env = ORIGINAL_ENV;
  });

  it('writes to the configured bucket when set', async () => {
    process.env.AGENT_FALLBACK_BUCKET = 'adp-dev-agent-run-logs-123456789012';

    const uri = await saveToS3Fallback(4184, 'comment', 'body');

    expect(mockSend).toHaveBeenCalledTimes(1);
    expect(mockPutObjectCommand).toHaveBeenCalledWith(
      expect.objectContaining({ Bucket: 'adp-dev-agent-run-logs-123456789012' })
    );
    expect(uri).toContain('s3://adp-dev-agent-run-logs-123456789012/');
  });

  it('attempts NO write at all when the bucket is unconfigured', async () => {
    // The assertion that matters: a doomed PutObject is what made this bug
    // invisible. An unconfigured fallback must skip loudly, not fail silently.
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});

    const uri = await saveToS3Fallback(4184, 'comment', 'body');

    expect(mockSend).not.toHaveBeenCalled();
    expect(mockPutObjectCommand).not.toHaveBeenCalled();
    expect(uri).toBeNull();
    expect(errorSpy).toHaveBeenCalledTimes(1);
    expect(errorSpy.mock.calls[0][0]).toContain('AGENT_FALLBACK_BUCKET');

    errorSpy.mockRestore();
  });

  it('never targets the foreign-account bucket when unconfigured', async () => {
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});

    await saveToS3Fallback(4184, 'comment', 'body');

    expect(mockPutObjectCommand).not.toHaveBeenCalledWith(
      expect.objectContaining({ Bucket: 'adp-agent-state' })
    );

    errorSpy.mockRestore();
  });

  it('namespaces the object key by owner and repo', async () => {
    process.env.AGENT_FALLBACK_BUCKET = 'adp-dev-agent-run-logs-123456789012';

    await saveToS3Fallback(4184, 'comment', 'body');

    expect(mockPutObjectCommand).toHaveBeenCalledWith(
      expect.objectContaining({
        Key: expect.stringMatching(/^agent-fallback\/aws-e\/adp\/issue-4184\//),
      })
    );
  });

  it('does not propagate a failed S3 write to the caller', async () => {
    // This runs inside an existing catch — throwing here would replace a
    // diagnosable "both paths failed" log with an unhandled rejection and lose
    // the original GitHub error too.
    process.env.AGENT_FALLBACK_BUCKET = 'adp-dev-agent-run-logs-123456789012';
    mockSend.mockRejectedValue(new Error('AccessDenied'));
    const errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});

    await expect(saveToS3Fallback(4184, 'comment', 'body')).resolves.toBeNull();

    errorSpy.mockRestore();
  });
});
