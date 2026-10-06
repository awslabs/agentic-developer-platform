import { AGENT_WORK_PROMPT, renderAgentWorkAnswer, WorkPresentation } from './answer';
import { activityTools } from './tools';
import { ChatDataClient } from '../gateway/chat-data-client';

const fixture: WorkPresentation = {
  status: 'partial',
  from: '2026-10-01T00:00:00Z', to: '2026-10-04T00:00:00Z', timezone: 'America/New_York',
  observed_at: '2026-10-05T00:00:00Z', last_key: null,
  runs: [
    { invocation_id: 'root', source_type: 'activity', trigger_kind: 'human', persona: 'developer',
      invoked_at: '2026-10-01T12:00:00Z', status: 'complete', repo: 'sample/widgets', issue_number: 14 },
    { invocation_id: 'child', source_type: 'activity', trigger_kind: 'agent', persona: 'reviewer',
      invoked_at: '2026-10-02T12:00:00Z', status: 'failed', error: 'Review failed', repo: 'sample/widgets', issue_number: 14 },
    { invocation_id: 'task', source_type: 'task', trigger_kind: 'human', persona: 'developer',
      invoked_at: '2026-10-03T12:00:00Z', status: 'complete', repo: 'sample/widgets', issue_number: 15 },
  ],
  issues: [
    { url: 'https://github.com/sample/widgets/issues/14', invocation_ids: ['root', 'child'] },
    { url: 'https://github.com/sample/widgets/issues/15', invocation_ids: ['task'] },
  ],
  coverage: [{ source: 'activity_descendants', status: 'unavailable', reason: 'index_missing' }],
};

it('cites each recorded completion and separates personal triggers from descendant work', () => {
  const answer = renderAgentWorkAnswer(fixture);
  expect(answer).toContain('Your personal triggers:');
  expect(answer).toContain('Agent work (including runs you triggered):');
  expect(answer).toContain('Descendant agent run (reviewer)');
  expect(answer).toContain('recorded error: Review failed');
  expect(answer).toContain('[issue](https://github.com/sample/widgets/issues/14)');
  expect(answer).toContain('Coverage partial: activity_descendants (index_missing)');
  const completionLines = answer.split('\n').filter(line => line.includes('recorded as complete'));
  expect(completionLines).toHaveLength(2);
  expect(completionLines[0]).toContain('[run root](/activity?id=root)');
  expect(completionLines[1]).toContain('[run task](/activity?id=task)');
  expect(answer).toContain('current issue state is unverified');
  expect(answer).not.toMatch(/issue (is|was) (closed|completed)/i);
  expect(AGENT_WORK_PROMPT).toContain('Every completion claim must cite');
});

it.each(['human', 'agent', 'bot'] as const)('links %s run citations to the activity detail route with an encoded id', triggerKind => {
  const invocationId = 'orch:run/with spaces?view=all&other=id#fragment+%';
  const answer = renderAgentWorkAnswer({
    ...fixture,
    runs: [{ ...fixture.runs[0], invocation_id: invocationId, trigger_kind: triggerKind }],
    issues: [],
  });
  const links = [...answer.matchAll(/\]\(([^)]+)\)/g)].map(match => match[1]);
  expect(links).toHaveLength(triggerKind === 'human' ? 2 : 1);
  for (const link of links) {
    expect(link).toBe(`/activity?id=${encodeURIComponent(invocationId)}`);
    const target = new URL(link, 'https://adp.example.test');
    expect(target.pathname).toBe('/activity');
    expect([...target.searchParams.entries()]).toEqual([['id', invocationId]]);
    expect(target.hash).toBe('');
  }
});

it('an unavailable empty read cannot become a no-work claim', () => {
  const answer = renderAgentWorkAnswer({ ...fixture, status: 'unavailable', runs: [], issues: [] });
  expect(answer).toContain('do not establish that no agent work occurred');
  expect(answer).not.toContain('No agent invocations were recorded');
});

it('the registered tool returns the cited answer alongside bounded source records', async () => {
  const sourceRuns = fixture.runs.map(run => ({ ...run, record_url: `/me/agent-invocations/${run.invocation_id}` }));
  const runRequest = jest.fn().mockResolvedValue({
    ...fixture, runs: sourceRuns, last_key: null, coverage: fixture.coverage,
  });
  const tool = activityTools({ runRequest } as unknown as ChatDataClient)[0];
  const response = JSON.parse((await tool.handler({ from: fixture.from, to: fixture.to, timezone: fixture.timezone })).content[0].text);
  expect(response.runs).toEqual(sourceRuns);
  expect(response.answer.split('\n').filter((line: string) => line.includes('recorded as complete'))).toHaveLength(2);
  expect(response.answer).toContain('[run task](/activity?id=task)');
  expect(response.answer).not.toContain('/me/agent-invocations/');
  expect(response.status).toBe('partial');
});
