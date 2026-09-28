import { createWorkerActivityLog } from './worker-activity-log';
import { CloudWatchLogsClient, CreateLogStreamCommand, PutLogEventsCommand } from '@aws-sdk/client-cloudwatch-logs';

jest.mock('@aws-sdk/client-cloudwatch-logs', () => ({
  CloudWatchLogsClient: jest.fn(), CreateLogStreamCommand: jest.fn(input => ({ kind: 'create', input })),
  PutLogEventsCommand: jest.fn(input => ({ kind: 'put', input })),
}));
jest.mock('./lib/runIdentity', () => ({ workerAwsCredentials: () => undefined, workerAwsRegion: () => 'us-east-1' }));
const send = jest.fn();
beforeEach(() => {
  jest.clearAllMocks();
  (CloudWatchLogsClient as unknown as jest.Mock).mockImplementation(() => ({ send }));
  send.mockResolvedValue({});
});

test.each(['developer', 'agent-codex-developer'])('preserves the Activity log contract for %s', async persona => {
  const log = createWorkerActivityLog(persona, '42');
  await log.start();
  log.log('INFO', 'Running tests', { invocation_id: 'run-42' });
  await log.flush();
  expect(CreateLogStreamCommand).toHaveBeenCalledWith({ logGroupName: '/adp/dev/agent-factory/agent', logStreamName: expect.stringMatching(new RegExp(`^agent-${persona}-issue-42-\\d+$`)) });
  const payload = (PutLogEventsCommand as unknown as jest.Mock).mock.calls[0][0];
  expect(JSON.parse(payload.logEvents[1].message)).toMatchObject({ agentType: persona, issueNumber: '42', message: 'Running tests', invocation_id: 'run-42' });
  await log.flush();
  expect(PutLogEventsCommand).toHaveBeenCalledTimes(1);
});
