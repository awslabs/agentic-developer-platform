import { GitHubClient } from './GitHubClient';
import { Logger } from './Logger';
import { ChecklistItem, Milestone } from '../types';

export class ProgressTracker {
  private githubClient: GitHubClient;
  private logger: Logger;
  private issueNumber: number;
  private checklistCommentId: number | null = null;
  private items: ChecklistItem[] = [];

  constructor(githubClient: GitHubClient, logger: Logger, issueNumber: number) {
    this.githubClient = githubClient;
    this.logger = logger;
    this.issueNumber = issueNumber;
  }

  async createChecklist(items: ChecklistItem[]): Promise<number> {
    this.items = items;
    const body = this.formatChecklist();
    this.checklistCommentId = await this.githubClient.createComment(this.issueNumber, body);
    this.logger.info('Checklist created', { component: 'ProgressTracker', commentId: this.checklistCommentId });
    return this.checklistCommentId;
  }

  async updateChecklistItem(index: number, completed: boolean): Promise<void> {
    if (!this.checklistCommentId || index >= this.items.length) return;
    
    this.items[index].completed = completed;
    const body = this.formatChecklist();
    await this.githubClient.updateComment(this.checklistCommentId, body);
    this.logger.debug('Checklist updated', { component: 'ProgressTracker', index, completed });
  }

  async postMilestoneComment(milestone: Milestone): Promise<void> {
    const body = `### ${milestone.name}\n\n${milestone.description}\n\n_${milestone.timestamp}_`;
    await this.githubClient.createComment(this.issueNumber, body);
    this.logger.info('Milestone posted', { component: 'ProgressTracker', milestone: milestone.name });
  }

  private formatChecklist(): string {
    const header = '## 🤖 Agent Progress\n\n';
    const list = this.items
      .map(item => `- [${item.completed ? 'x' : ' '}] ${item.label}`)
      .join('\n');
    const completed = this.items.filter(i => i.completed).length;
    const footer = `\n\n_Progress: ${completed}/${this.items.length}_`;
    return header + list + footer;
  }

  getChecklistCommentId(): number | null {
    return this.checklistCommentId;
  }

  setChecklistCommentId(id: number): void {
    this.checklistCommentId = id;
  }
}
