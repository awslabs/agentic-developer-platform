/** Native issue relationships, with host-owned transport and durable effect journal. */
import { z } from 'zod';
import { readyAssignments, storyMarker, type BacklogStory, type PlanningArtifact } from './planning.js';

export interface PlanningTransport {
  read(path: string): Promise<unknown>;
  write(path: string, body: Record<string, unknown>): Promise<unknown>;
  /** Completed effects return their receipt; uncertain effects must never be replayed. */
  effect(key: string, request: unknown, execute: () => Promise<unknown>): Promise<unknown>;
  dispatch(issue: number, persona: string, reason: string): Promise<unknown>;
}
const number = z.number().int().positive();
const githubIssue = z.object({ id: number, number, state: z.enum(['open', 'closed']), state_reason: z.string().nullable().optional(),
  title: z.string(), body: z.string().nullable(), pull_request: z.unknown().optional() });
const gitlabIssue = z.object({ id: number, iid: number, state: z.enum(['opened', 'closed']), title: z.string(), description: z.string().nullable() });

export class PlanningProvider {
  private root: string;
  private repository: string;
  constructor(readonly provider: 'github' | 'gitlab', repository: string, readonly parent: number, private host: PlanningTransport) {
    number.parse(parent);
    this.repository = repository;
    if (!/^[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.-]+)+$/.test(repository) || repository.split('/').some(p => p === '.' || p === '..')) throw new Error('Invalid repository');
    this.root = provider === 'github' ? `/repos/${repository}/issues` : `/projects/${encodeURIComponent(repository)}/issues`;
  }
  private issue(raw: unknown) {
    if (this.provider === 'github') {
      const value = githubIssue.parse(raw);
      const location = z.object({ repository_url: z.string().optional() }).parse(raw).repository_url;
      if (location && location !== `https://api.github.com/repos/${this.repository}`) throw new Error('Cross-repository story is outside scope');
      if (value.pull_request) throw new Error('Expected an issue, not a pull request');
      return { id: value.id, issue: value.number, title: value.title, body: value.body ?? '',
        state: value.state === 'open' ? 'open' as const : value.state_reason === 'completed' ? 'completed' as const : 'cancelled' as const };
    }
    const value = gitlabIssue.parse(raw);
    return { id: value.id, issue: value.iid, title: value.title, body: value.description ?? '', state: value.state === 'opened' ? 'open' as const : 'completed' as const };
  }
  async children() {
    const raw = await this.host.read(`${this.root}/${this.parent}/${this.provider === 'github' ? 'sub_issues' : 'links'}?per_page=100`);
    const items = z.array(z.unknown()).max(99).parse(raw); // A full page needs pagination, never silently assume completeness.
    return items.map(item => this.issue(item)).filter(item => this.provider === 'github' || item.body.includes(`ADP parent: ${this.parent}\n`));
  }
  async backlog(onlyIssue?: number): Promise<BacklogStory[]> {
    const children = (await this.children()).filter(child => onlyIssue === undefined || child.issue === onlyIssue);
    const result: BacklogStory[] = [];
    const external = new Set<number>();
    for (const child of children) {
      const raw = await this.host.read(`${this.root}/${child.issue}/${this.provider === 'github' ? 'dependencies/blocked_by' : 'links'}?per_page=100`);
      const links = z.array(z.record(z.string(), z.unknown())).max(99).parse(raw);
      const blockers = links.filter(link => this.provider === 'github' || link.link_type === 'is_blocked_by').map(link => this.issue(link).issue);
      for (const blocker of blockers) if (!children.some(c => c.issue === blocker)) external.add(blocker);
      let assigned = false;
      if (this.provider === 'github') {
        const timeline = z.array(z.record(z.string(), z.unknown())).max(99).parse(await this.host.read(`${this.root}/${child.issue}/timeline?per_page=100`));
        assigned = timeline.some(event => {
          const source = event.source as { issue?: { state?: string; pull_request?: unknown } } | undefined;
          return event.event === 'cross-referenced' && source?.issue?.state === 'open' && !!source.issue.pull_request;
        });
      } else {
        const changes = z.array(z.object({state: z.string()})).max(99).parse(await this.host.read(`${this.root}/${child.issue}/related_merge_requests?per_page=100`));
        assigned = changes.some(change => change.state === 'opened');
      }
      result.push({ issue: child.issue, state: child.state, blockedBy: blockers, assigned });
    }
    if (external.size > 100) throw new Error('Dependency graph exceeds bound');
    for (const id of external) {
      const issue = this.issue(await this.host.read(`${this.root}/${id}`));
      result.push({ issue: id, state: issue.state, blockedBy: [] });
    }
    return result;
  }
  async publishStories(artifact: Extract<PlanningArtifact, { stories: unknown }>) {
    const receipts = new Map<string, ReturnType<PlanningProvider['issue']>>();
    for (const story of artifact.stories) {
      const body = `ADP parent: ${this.parent}\n\n${story.description}\n\nAcceptance criteria:\n${story.acceptance_criteria.map(c => `- ${c}`).join('\n')}\n\nSources: ${story.source_refs.join(', ')}\n\n${storyMarker(this.parent, story.key)}`;
      const request = { title: story.title, body: body.replace(/@/g, '@\u200b') };
      const raw = await this.host.effect(`story-create:${story.key}`, request, () => this.host.write(this.root,
        this.provider === 'github' ? request : { title: request.title, description: request.body }));
      const issue = this.issue(raw); receipts.set(story.key, issue);
      await this.host.effect(`story-link:${story.key}`, { child: issue.id }, () => this.host.write(
        `${this.root}/${this.parent}/${this.provider === 'github' ? 'sub_issues' : 'links'}`,
        this.provider === 'github' ? { sub_issue_id: issue.id } : { target_issue_iid: issue.issue, link_type: 'relates_to' }));
    }
    for (const story of artifact.stories) for (const blocker of story.blocked_by) {
      const child = receipts.get(story.key)!, dependency = receipts.get(blocker)!;
      await this.host.effect(`story-blocker:${story.key}:${blocker}`, { child: child.id, blocker: dependency.id }, () => this.host.write(
        `${this.root}/${child.issue}/${this.provider === 'github' ? 'dependencies/blocked_by' : 'links'}`,
        this.provider === 'github' ? { issue_id: dependency.id } : { target_issue_iid: dependency.issue, link_type: 'is_blocked_by' }));
    }
    return [...receipts].map(([key, value]) => ({ key, issue: value.issue }));
  }
  async schedule(assignments: readonly { issue: number; persona: string }[]) {
    if (new Set(assignments.map(a => a.issue)).size !== assignments.length) throw new Error("Duplicate scheduling identity");
    const result: Array<{ issue: number; status: string; receipt?: unknown }> = [];
    for (const assignment of assignments) {
      // Re-read the target, native membership, blockers and open changes immediately before dispatch.
      const eligible = readyAssignments(await this.backlog(assignment.issue), [assignment]).length === 1;
      if (!eligible) { result.push({ issue: assignment.issue, status: 'blocked_or_ineligible' }); continue; }
      if (!['codex-developer'].includes(assignment.persona)) throw new Error('Unsupported planning assignee');
      const receipt = await this.host.effect(`dispatch:${assignment.issue}`, assignment,
        () => this.host.dispatch(assignment.issue, assignment.persona, `PM scheduling child ${assignment.issue} of ${this.parent}`));
      result.push({ issue: assignment.issue, status: 'dispatched', receipt });
    }
    return result;
  }
}
