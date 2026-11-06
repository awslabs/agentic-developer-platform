jest.mock('@anthropic-ai/claude-agent-sdk', () => ({ query: jest.fn() }));

import { AgentOrchestrator } from './AgentOrchestrator';
import { bindPostedApproval } from '../services/posted-approval';
import type { IssueContext, ApprovalOutcome } from '../types';

const issue: IssueContext = { owner: 'owner', repo: 'repo', issueNumber: 42,
  issueTitle: 'Fixture issue', issueBody: 'Fixture body', labels: [] };

describe('SCN-04 orchestrator implementation gate', () => {
  it.each(['allowed-once', 'rejected', 'unavailable'] as ApprovalOutcome[])(
    'starts implementation only after %s', async outcome => {
      const logger = { info: jest.fn(), warn: jest.fn(), error: jest.fn() };
      const githubClient = { createComment: jest.fn(async () => 1) };
      const workspaceManager = { createWorkspace: jest.fn(async () => '/tmp/fixture'),
        getRepoPath: jest.fn(() => '/tmp/fixture/repo'), cleanup: jest.fn(async () => {}) };
      const stateManager = { saveState: jest.fn(async () => {}) };
      const approvalService = { pollForApproval: jest.fn(async () => ({ outcome })) };
      const orchestrator = new AgentOrchestrator(logger as any, githubClient as any, stateManager as any,
        workspaceManager as any, approvalService as any, { handleError: jest.fn() } as any, 42);
      const host = orchestrator as any;
      const postedAt = new Date().toISOString();
      const body = 'This run and exact plan';
      const receipt = { id: '100', body, createdAt: postedAt, updatedAt: postedAt };
      host.handlePlanning = jest.fn(async () => {
        const requestId = host.approvalRequestId(0);
        host.pendingApproval = bindPostedApproval(requestId, host.runId, 'owner', 'repo', 42,
          body, receipt, 30 * 60_000);
      });
      host.progressTracker.updateChecklistItem = jest.fn(async () => {});
      host.handleCodeGeneration = jest.fn(async () => {});
      host.createPullRequest = jest.fn(async () => {});

      await orchestrator.run(issue);

      expect(approvalService.pollForApproval).toHaveBeenCalledWith(42, expect.objectContaining({
        planCommentId: '100', runId: host.runId, state: 'pending',
      }));
      expect(host.handleCodeGeneration).toHaveBeenCalledTimes(outcome === 'allowed-once' ? 1 : 0);
      expect(host.createPullRequest).toHaveBeenCalledTimes(outcome === 'allowed-once' ? 1 : 0);
      expect(workspaceManager.cleanup).toHaveBeenCalledTimes(1);
    },
  );
});
