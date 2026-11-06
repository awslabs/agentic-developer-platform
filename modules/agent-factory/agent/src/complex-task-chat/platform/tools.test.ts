import { ChatDataClient, ChatDataError } from '../gateway/chat-data-client';
import { EvidenceCollector, PLATFORM_TOOL_INPUTS, platformTools, toolListingSchema } from './tools';

const stamp = '2026-10-04T12:00:00Z';
const listing = toolListingSchema.parse({
  tools: [
    { name: 'platform___my_work_items', description: 'Work items', inputSchema: { type: 'object' }, kind: 'first_party', read_only: true },
    { name: 'platform___my_flows', description: 'Flows', inputSchema: { type: 'object' }, kind: 'first_party', read_only: true },
  ],
  withheld: [], coverage: [], policy_revision: 1, observed_at: stamp,
});

function client(runRequest: jest.Mock) {
  return { runRequest } as unknown as ChatDataClient;
}

describe('first-party platform tools (#8113)', () => {
  it('registers known tools with the client-side strict input contracts and calls the data plane with a fresh request id', async () => {
    const runRequest = jest.fn().mockResolvedValue({
      tool: 'platform___my_work_items', receipt_id: 'r1', request_id: 'ignored', status: 'confirmed',
      result: { source: 'github.work_items', observed_at: stamp, window: { from: '2026-09-04T00:00:00Z', to: stamp }, has_more: true,
        coverage: [{ source: 'github.work_items', status: 'partial', reason: 'repository_page_limit' }, { source: 'github.work_items', status: 'available' }],
        items: [{ kind: 'pull_request', repo: 'acme/widgets', number: 7, title: 'Fix', url: 'https://github.com/acme/widgets/pull/7' },
          { kind: 'issue', repo: 'acme/widgets', number: 8, title: 'Bad link', url: 'javascript:alert(1)' }] },
    });
    const evidence = new EvidenceCollector();
    const tools = platformTools(client(runRequest), listing, evidence);
    expect(tools.map(tool => tool.name)).toEqual(['platform___my_work_items', 'platform___my_flows']);
    expect(Object.keys(tools[0].inputSchema).sort()).toEqual(Object.keys(PLATFORM_TOOL_INPUTS.platform___my_work_items).sort());
    const result = await tools[0].handler({ state: 'open', limit: 5 });
    expect(runRequest).toHaveBeenCalledWith('tools/call', {
      tool: 'platform___my_work_items', arguments: { state: 'open', limit: 5 }, request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
    });
    expect(result.isError).toBeUndefined();
    expect(JSON.parse(result.content[0].text).items).toHaveLength(2);
    expect(evidence.compose()).toEqual({
      status: 'partial', has_more: true, from: '2026-09-04T00:00:00Z', to: stamp, timezone: 'UTC', observed_at: stamp,
      coverage: [
        { source: 'platform___my_work_items:github.work_items', status: 'partial', reason: 'repository_page_limit' },
        { source: 'platform___my_work_items:github.work_items', status: 'available' },
      ],
      // Only https links are offered to the footer; the javascript: URL is dropped, never rewritten.
      sources: [{ label: 'acme/widgets#7', url: 'https://github.com/acme/widgets/pull/7' }],
    });
  });

  it('reports a refusal or outage as a readable tool error and as denied/unavailable coverage, but not a broken capability', async () => {
    const evidence = new EvidenceCollector();
    const refused = new ChatDataError('denied', 403, undefined, undefined, undefined, 'tool_not_consented');
    const down = new ChatDataError('unavailable', 503);
    const expired = new ChatDataError('denied', 401, undefined, 'capability_expired');
    const runRequest = jest.fn().mockRejectedValueOnce(refused).mockRejectedValueOnce(down).mockRejectedValueOnce(expired);
    const [workItems] = platformTools(client(runRequest), listing, evidence);
    expect(await workItems.handler({})).toEqual({ isError: true, content: [{ type: 'text',
      text: 'platform___my_work_items was refused (tool_not_consented). Report this gap to the owner instead of guessing.' }] });
    expect(await workItems.handler({})).toEqual({ isError: true, content: [{ type: 'text',
      text: 'platform___my_work_items is unavailable (unavailable). Report this gap to the owner instead of guessing.' }] });
    await expect(workItems.handler({})).rejects.toBe(expired);
    expect(evidence.compose()).toEqual({ status: 'unavailable', has_more: false, sources: [], coverage: [
      { source: 'platform___my_work_items', status: 'denied', reason: 'tool_not_consented' },
      { source: 'platform___my_work_items', status: 'unavailable', reason: 'unavailable' },
    ] });
  });

  it('refuses a result for another tool or an unbounded shape, and bounds oversized rows by truncating with partial coverage', async () => {
    const evidence = new EvidenceCollector();
    const runRequest = jest.fn()
      .mockResolvedValueOnce({ tool: 'platform___my_flows', receipt_id: 'r', request_id: 'q', status: 'confirmed',
        result: { source: 'orchestration.flows', observed_at: stamp, coverage: [], flows: [] } })
      .mockResolvedValueOnce({ tool: 'platform___my_work_items', receipt_id: 'r', request_id: 'q', status: 'confirmed',
        result: { source: 'github.work_items', observed_at: stamp, coverage: [{ source: 'github.work_items', status: 'available' }],
          items: Array.from({ length: 400 }, (_, index) => ({ kind: 'issue', repo: 'acme/widgets', number: index, title: 'x'.repeat(190),
            url: `https://github.com/acme/widgets/issues/${index}` })) } });
    const [workItems] = platformTools(client(runRequest), listing, evidence);
    await expect(workItems.handler({})).rejects.toMatchObject({ code: 'invalid_response' });
    const result = JSON.parse((await workItems.handler({})).content[0].text);
    expect(result.items.length).toBeLessThan(400);
    expect(JSON.stringify(result).length).toBeLessThanOrEqual(24_000);
    expect(result.has_more).toBe(true);
    expect(result.coverage.at(-1)).toEqual({ source: 'github.work_items', status: 'partial', reason: 'result_truncated' });
    expect(evidence.compose()?.status).toBe('partial');
    expect(evidence.compose()?.sources.length).toBeLessThanOrEqual(32);
  });

  it('skips a listed tool this build cannot call and composes empty / denied / ok statuses honestly', () => {
    const evidence = new EvidenceCollector();
    const unknown = toolListingSchema.parse({ ...listing, tools: [{ ...listing.tools[0], name: 'platform___unknown' }] });
    const log = jest.spyOn(console, 'error').mockImplementation(() => undefined);
    expect(platformTools(client(jest.fn()), unknown, evidence)).toEqual([]);
    expect(log).toHaveBeenCalledTimes(1);
    log.mockRestore();
    expect(evidence.compose()).toBeUndefined();
    evidence.record('platform___my_flows', { source: 'orchestration.flows', observed_at: stamp, coverage: [{ source: 'orchestration.flows', status: 'empty' }] });
    expect(evidence.compose()?.status).toBe('empty');
    const denied = new EvidenceCollector();
    denied.failure('platform___my_budget', 'denied', 'tool_not_consented');
    expect(denied.compose()?.status).toBe('denied');
    const ok = new EvidenceCollector();
    ok.record('platform___my_budget', { source: 'budget.me', observed_at: stamp, coverage: [{ source: 'budget.me', status: 'available' }] });
    expect(ok.compose()).toEqual({ status: 'ok', has_more: false, observed_at: stamp, sources: [], coverage: [{ source: 'platform___my_budget:budget.me', status: 'available' }] });
  });
});
