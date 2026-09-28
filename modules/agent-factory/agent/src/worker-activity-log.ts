/** Shared CloudWatch log contract consumed by Agent Activity for both SDKs. */
import { CloudWatchLogsClient, CreateLogStreamCommand, PutLogEventsCommand } from '@aws-sdk/client-cloudwatch-logs';
import { workerAwsCredentials, workerAwsRegion } from './lib/runIdentity';
import { resolveAgentLogGroup } from './lib/logGroup';

export function createWorkerActivityLog(persona: string, issueNumber: string) {
  const logGroupName = resolveAgentLogGroup();
  const logStreamName = `agent-${persona}-issue-${issueNumber}-${Date.now()}`;
  const client = new CloudWatchLogsClient({ region: workerAwsRegion(), credentials: workerAwsCredentials() });
  const buffer: { timestamp: number; message: string }[] = [];
  let initialized = false;
  const log = (level: string, message: string, context?: Record<string, unknown>) => {
    const entry = { level, message, issueNumber, agentType: persona, ...context, timestamp: new Date().toISOString() };
    const emoji = level === 'ERROR' ? '❌' : level === 'WARN' ? '⚠️' : '→';
    console.log(`${emoji} [${persona}] ${message}`);
    if (initialized) buffer.push({ timestamp: Date.now(), message: JSON.stringify(entry) });
  };
  return {
    log,
    async start() {
      try {
        await client.send(new CreateLogStreamCommand({ logGroupName, logStreamName }));
        initialized = true;
        log('INFO', `CloudWatch logging initialized for @${persona.startsWith('agent-') ? persona : `agent-${persona}`}`);
      } catch (error) {
        if ((error as Error).name === 'ResourceAlreadyExistsException') initialized = true;
        else console.warn('CloudWatch init failed:', (error as Error).message);
      }
    },
    async flush() {
      if (!initialized || !buffer.length) return;
      const logEvents = buffer.splice(0, buffer.length);
      try { await client.send(new PutLogEventsCommand({ logGroupName, logStreamName, logEvents })); }
      catch (error) { console.warn('CloudWatch flush failed:', (error as Error).message); }
    },
  };
}
