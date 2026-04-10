import * as fs from 'fs/promises';
import { IssueContext } from '../types';
import { Logger } from './Logger';

export class InputHandler {
  private logger: Logger;

  constructor(logger: Logger) {
    this.logger = logger;
  }

  async getIssueContext(): Promise<IssueContext> {
    const eventPath = process.env.GITHUB_EVENT_PATH;
    if (!eventPath) throw new Error('GITHUB_EVENT_PATH not set');

    const eventData = JSON.parse(await fs.readFile(eventPath, 'utf-8'));
    return this.parseGitHubEvent(eventData);
  }

  parseGitHubEvent(payload: Record<string, unknown>): IssueContext {
    const issue = (payload.issue || payload.pull_request) as Record<string, unknown>;
    const repo = payload.repository as Record<string, unknown>;
    const owner = (repo.owner as Record<string, unknown>).login as string;

    const context: IssueContext = {
      owner,
      repo: repo.name as string,
      issueNumber: issue.number as number,
      issueTitle: issue.title as string,
      issueBody: (issue.body as string) || '',
      labels: ((issue.labels as { name: string }[]) || []).map(l => l.name),
    };

    this.validateInput(context);
    this.logger.info('Issue context parsed', { component: 'InputHandler', issueNumber: context.issueNumber });
    return context;
  }

  validateInput(context: IssueContext): void {
    if (!context.owner) throw new Error('Missing owner');
    if (!context.repo) throw new Error('Missing repo');
    if (!context.issueNumber) throw new Error('Missing issue number');
  }
}
