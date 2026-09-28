import { CloudWatchLogsClient, PutLogEventsCommand, CreateLogStreamCommand } from '@aws-sdk/client-cloudwatch-logs';
import { Config, LogContext } from '../types';

export class Logger {
  private client: CloudWatchLogsClient;
  private logGroupName = '/github-ccsdk-agent/logs';
  private logStreamName: string;
  private sequenceToken: string | undefined;
  private config: Config;
  private buffer: { timestamp: number; message: string }[] = [];
  private flushInterval: NodeJS.Timeout | null = null;

  constructor(config: Config, issueId: string) {
    this.config = config;
    this.client = new CloudWatchLogsClient({ region: config.awsRegion });
    this.logStreamName = `agent-${issueId}-${Date.now()}`;
  }

  async initialize(): Promise<void> {
    try {
      await this.client.send(new CreateLogStreamCommand({
        logGroupName: this.logGroupName,
        logStreamName: this.logStreamName,
      }));
    } catch (err: unknown) {
      if ((err as { name?: string }).name !== 'ResourceAlreadyExistsException') throw err;
    }

    this.flushInterval = setInterval(() => this.flush(), 5000);
  }

  private shouldLog(level: Config['logLevel']): boolean {
    const levels: Config['logLevel'][] = ['DEBUG', 'INFO', 'WARN', 'ERROR'];
    return levels.indexOf(level) >= levels.indexOf(this.config.logLevel);
  }

  private log(level: Config['logLevel'], message: string, context?: LogContext): void {
    if (!this.shouldLog(level)) return;

    const logEntry = {
      level,
      message: this.maskSecrets(message),
      ...this.maskContextSecrets(context),
      timestamp: new Date().toISOString(),
    };

    console.log(JSON.stringify(logEntry));
    this.buffer.push({ timestamp: Date.now(), message: JSON.stringify(logEntry) });
  }

  private maskSecrets(text: string): string {
    return text
      .replace(/ghp_[a-zA-Z0-9]{36}/g, 'ghp_***MASKED***')
      .replace(/ghs_[a-zA-Z0-9]{36}/g, 'ghs_***MASKED***')
      .replace(/github_pat_[a-zA-Z0-9_]{22,}/g, 'github_pat_***MASKED***')
      .replace(/x-access-token:[^@]+@/g, 'x-access-token:***MASKED***@')
      .replace(/Bearer\s+[a-zA-Z0-9\-_.]+/gi, 'Bearer ***MASKED***');
  }

  private maskContextSecrets(context?: LogContext): LogContext | undefined {
    if (!context) return context;
    const masked: LogContext = {};
    for (const [key, value] of Object.entries(context)) {
      masked[key] = typeof value === 'string' ? this.maskSecrets(value) : value;
    }
    return masked;
  }

  debug(message: string, context?: LogContext): void {
    this.log('DEBUG', message, context);
  }

  info(message: string, context?: LogContext): void {
    this.log('INFO', message, context);
  }

  warn(message: string, context?: LogContext): void {
    this.log('WARN', message, context);
  }

  error(message: string, error?: Error, context?: LogContext): void {
    this.log('ERROR', message, { ...context, error: error?.message, stack: error?.stack });
  }

  async flush(): Promise<void> {
    if (this.buffer.length === 0) return;

    const events = this.buffer.splice(0, this.buffer.length);
    try {
      const response = await this.client.send(new PutLogEventsCommand({
        logGroupName: this.logGroupName,
        logStreamName: this.logStreamName,
        logEvents: events,
        sequenceToken: this.sequenceToken,
      }));
      this.sequenceToken = response.nextSequenceToken;
    } catch (err) {
      console.error('Failed to flush logs:', err);
    }
  }

  async close(): Promise<void> {
    if (this.flushInterval) clearInterval(this.flushInterval);
    await this.flush();
  }
}
