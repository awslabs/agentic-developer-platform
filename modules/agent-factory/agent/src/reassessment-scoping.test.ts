/**
 * Regression tests for sub-issue scoping in reassessment.
 *
 * Background: the PM agent reported EPIC #308 as having "9 completed tasks"
 * when in fact #308 had zero sub-issues. The completed tasks it listed
 * belonged to EPIC #181 — they were present on the shared project board.
 *
 * Two bugs caused this:
 *
 * 1. `findChildIssues` fell through to a text-search fallback when the
 *    GraphQL sub-issues API returned an empty result. The fallback matched
 *    any issue whose body contained "#<parent>", returning false-positive
 *    "children" (issues that merely *referenced* the parent as a dependency
 *    or in documentation).
 *
 * 2. `buildStateAnalysis` took the full unfiltered `projectBoardItems`
 *    (which is the entire project's items across every EPIC) and counted
 *    every "Done" item as a `completedTask` for the EPIC being reassessed.
 *
 * These tests lock in the fix: an EPIC with zero real sub-issues must
 * report zero completed / in-progress / pending / blocked tasks, even if
 * the project board contains many items from other EPICs.
 */

import { buildStateAnalysis, findChildIssues, ChildIssue, ProjectBoardItem, IssueComment } from './reassessment';

describe('reassessment scoping', () => {
  describe('buildStateAnalysis — scoping projectBoardItems to childIssues', () => {
    const epicHasNoChildren: ChildIssue[] = [];

    // The shared project board contains 9 items from a DIFFERENT EPIC.
    // None of them are sub-issues of the EPIC being reassessed.
    const otherEpicsItemsOnBoard: ProjectBoardItem[] = [
      { issueNumber: 184, title: 'Stage A: JWT claims', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
      { issueNumber: 185, title: 'Stage B: Catalog schema', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
      { issueNumber: 186, title: 'Stage C: S3 key layout', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
      { issueNumber: 187, title: 'Stage D: Quota hooks', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
      { issueNumber: 203, title: 'Stage A verification', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
      { issueNumber: 210, title: 'Stage B+C sanity', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
      { issueNumber: 219, title: 'File attachment E2E', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
      { issueNumber: 222, title: 'Playwright UI', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
      { issueNumber: 211, title: 'Infra bug fix', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
    ];

    const noComments: IssueComment[] = [];

    it('reports zero completed tasks when the EPIC has no sub-issues (regression #308)', () => {
      const analysis = buildStateAnalysis(noComments, epicHasNoChildren, otherEpicsItemsOnBoard);

      expect(analysis.completedTasks).toEqual([]);
      expect(analysis.completedTasks.length).toBe(0);
    });

    it('reports zero in-progress tasks when the EPIC has no sub-issues', () => {
      const mixedStatusBoard: ProjectBoardItem[] = [
        ...otherEpicsItemsOnBoard,
        { issueNumber: 999, title: 'Other EPIC in-progress', status: 'In Progress', assignedAgent: null, blockedBy: null, workflowRun: null },
      ];
      const analysis = buildStateAnalysis(noComments, epicHasNoChildren, mixedStatusBoard);

      expect(analysis.inProgressTasks).toEqual([]);
    });

    it('reports zero pending tasks when the EPIC has no sub-issues', () => {
      const backlogItems: ProjectBoardItem[] = [
        { issueNumber: 101, title: 'Other backlog A', status: 'Backlog', assignedAgent: null, blockedBy: null, workflowRun: null },
        { issueNumber: 102, title: 'Other todo B', status: 'Todo', assignedAgent: null, blockedBy: null, workflowRun: null },
      ];
      const analysis = buildStateAnalysis(noComments, epicHasNoChildren, backlogItems);

      expect(analysis.pendingTasks).toEqual([]);
    });

    it('includes only items whose issueNumber is a child of the EPIC', () => {
      // EPIC has two real children, one of which is Done. Board has both
      // plus 9 items from other EPICs.
      const ourChildren: ChildIssue[] = [
        { number: 500, title: 'Our child A', state: 'CLOSED', labels: [], assignees: [] },
        { number: 501, title: 'Our child B', state: 'OPEN', labels: [], assignees: [] },
      ];
      const boardWithMix: ProjectBoardItem[] = [
        ...otherEpicsItemsOnBoard,
        { issueNumber: 500, title: 'Our child A', status: 'Done', assignedAgent: null, blockedBy: null, workflowRun: null },
        { issueNumber: 501, title: 'Our child B', status: 'In Progress', assignedAgent: null, blockedBy: null, workflowRun: null },
      ];

      const analysis = buildStateAnalysis(noComments, ourChildren, boardWithMix);

      expect(analysis.completedTasks).toEqual([500]);
      expect(analysis.inProgressTasks).toEqual([501]);
      expect(analysis.pendingTasks).toEqual([]);
      // None of the other EPIC's items leak in
      expect(analysis.completedTasks).not.toContain(184);
      expect(analysis.completedTasks).not.toContain(185);
      expect(analysis.completedTasks).not.toContain(210);
    });

    it('handles the case where a child issue is not on the board at all', () => {
      const ourChildren: ChildIssue[] = [
        { number: 500, title: 'Our child A', state: 'OPEN', labels: [], assignees: [] },
      ];
      // Board has items for OTHER EPICs, none for ours.
      const analysis = buildStateAnalysis(noComments, ourChildren, otherEpicsItemsOnBoard);

      // Child #500 isn't on the board → it's not in any status bucket
      expect(analysis.completedTasks).toEqual([]);
      expect(analysis.inProgressTasks).toEqual([]);
      expect(analysis.pendingTasks).toEqual([]);
    });
  });

  describe('findChildIssues — no text-search fallback on empty result', () => {
    // Mock execCommand that simulates GraphQL API returning empty sub-issues list.
    // If the bug regresses, the function would fall through to `gh issue list`
    // text search — which the mock will reveal.
    it('returns empty array when the GraphQL API returns zero sub-issues (no text-search fallback)', async () => {
      const calledCommands: string[] = [];
      const mockExec = async (cmd: string): Promise<string> => {
        calledCommands.push(cmd);

        if (cmd.includes('gh api graphql') && cmd.includes('subIssues')) {
          // API succeeds, but parent has no sub-issues
          return '[]';
        }
        // Any other call (e.g. `gh issue list --search ...`) means the fallback
        // was triggered — that would be the bug.
        throw new Error(`Unexpected command (fallback regression): ${cmd}`);
      };

      const result = await findChildIssues('308', 'aws-e', 'adp', mockExec);

      expect(result).toEqual([]);
      expect(calledCommands.length).toBe(1);
      expect(calledCommands[0]).toContain('subIssues');
      // Crucially, `gh issue list --search ... in:body` must NOT have been invoked.
      const fallbackCalls = calledCommands.filter(c => c.includes('in:body'));
      expect(fallbackCalls).toEqual([]);
    });

    it('returns actual sub-issues when the GraphQL API returns them', async () => {
      const mockExec = async (cmd: string): Promise<string> => {
        if (cmd.includes('gh api graphql') && cmd.includes('subIssues')) {
          return JSON.stringify([
            { number: 501, title: 'Child A', state: 'OPEN', labels: { nodes: [] }, assignees: { nodes: [] } },
            { number: 502, title: 'Child B', state: 'CLOSED', labels: { nodes: [] }, assignees: { nodes: [] } },
          ]);
        }
        throw new Error(`Unexpected command: ${cmd}`);
      };

      const result = await findChildIssues('308', 'aws-e', 'adp', mockExec);

      expect(result.length).toBe(2);
      expect(result[0].number).toBe(501);
      expect(result[0].state).toBe('OPEN');
      expect(result[1].number).toBe(502);
      expect(result[1].state).toBe('CLOSED');
    });

    it('returns empty array when the GraphQL call throws (graceful degradation)', async () => {
      const mockExec = async (_cmd: string): Promise<string> => {
        throw new Error('network failure');
      };

      const result = await findChildIssues('308', 'aws-e', 'adp', mockExec);

      expect(result).toEqual([]);
    });
  });
});
