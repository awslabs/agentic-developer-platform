import { activityTools } from './tools';
import { ChatDataClient } from '../gateway/chat-data-client';

it('follows empty filtered pages, preserves runs and deduplicates issue presentation', async () => {
  const runRequest = jest.fn()
    .mockResolvedValueOnce({ runs: [], issues: [], last_key: 'page-two' })
    .mockResolvedValueOnce({
      runs: [{ invocation_id: 'root', source_type: 'activity' }],
      issues: [{ url: 'https://github.com/sample/widgets/issues/14', invocation_ids: ['root'] }],
      last_key: 'page-three',
    })
    .mockResolvedValueOnce({
      runs: [{ invocation_id: 'child', source_type: 'activity' }],
      issues: [{ url: 'https://github.com/sample/widgets/issues/14', invocation_ids: ['child'] }],
      last_key: null,
    });
  const tool = activityTools({ runRequest } as unknown as ChatDataClient)[0];
  expect(tool.name).toBe('get_my_agent_work');
  expect(Object.keys(tool.inputSchema).sort()).toEqual(['from', 'last_key', 'page_size', 'timezone', 'to']);
  const window = { from: '2026-10-01T00:00:00Z', to: '2026-10-04T00:00:00Z', timezone: 'UTC' };
  const result = JSON.parse((await tool.handler(window)).content[0].text);
  expect(runRequest).toHaveBeenNthCalledWith(1, 'activity/work', window);
  expect(runRequest).toHaveBeenNthCalledWith(2, 'activity/work', { ...window, last_key: 'page-two' });
  expect(runRequest).toHaveBeenNthCalledWith(3, 'activity/work', { ...window, last_key: 'page-three' });
  expect(result.runs.map((run: { invocation_id: string }) => run.invocation_id)).toEqual(['root', 'child']);
  expect(result.issues).toEqual([{ url: 'https://github.com/sample/widgets/issues/14', invocation_ids: ['root', 'child'] }]);
  expect(result.last_key).toBeNull();
});

it('rejects a repeated cursor rather than claim the timeline is complete', async () => {
  const runRequest = jest.fn().mockResolvedValue({ runs: [], issues: [], last_key: 'loop' });
  const tool = activityTools({ runRequest } as unknown as ChatDataClient)[0];
  await expect(tool.handler({ from: '2026-10-01T00:00:00Z', to: '2026-10-04T00:00:00Z', timezone: 'UTC' }))
    .rejects.toMatchObject({ code: 'invalid_response' });
  expect(runRequest).toHaveBeenCalledTimes(2);
});

it('preserves partial coverage and cannot claim no work when sources fail', async () => {
  const runRequest = jest.fn().mockResolvedValue({
    status: 'unavailable', runs: [], issues: [], last_key: null, observed_at: '2026-10-05T00:00:00Z',
    coverage: [{ source: 'activity_direct', status: 'unavailable', reason: 'index_missing' }],
  });
  const tool = activityTools({ runRequest } as unknown as ChatDataClient)[0];
  const result = JSON.parse((await tool.handler({ from: '2026-10-01T00:00:00Z', to: '2026-10-04T00:00:00Z', timezone: 'UTC' })).content[0].text);
  expect(result.status).toBe('unavailable');
  expect(result.runs).toEqual([]);
  expect(result.coverage).toEqual([{ source: 'activity_direct', status: 'unavailable', reason: 'index_missing' }]);
  expect(result.observed_at).toBe('2026-10-05T00:00:00Z');
});

it.each([false, true])('keeps resumed empty results page-scoped after the twenty-page limit (gateway marker: %s)', async gatewayMarker => {
  const available = { source: 'activity_direct', status: 'available', reason: 'queried' };
  const missing = [
    { source: 'activity_descendants', status: 'unavailable', reason: 'index_missing' },
    { source: 'tasks', status: 'unavailable', reason: 'provider_failure' },
  ];
  const continuation = { source: 'pagination', status: 'partial', reason: 'continuation_only' };
  let pageNumber = 0;
  const runRequest = jest.fn().mockImplementation(async () => {
    pageNumber++;
    return {
      runs: [], issues: [], last_key: pageNumber < 21 ? `page-${pageNumber + 1}` : null,
      coverage: [available, ...(pageNumber === 1 ? missing : gatewayMarker ? [continuation] : [])],
    };
  });
  const tool = activityTools({ runRequest } as unknown as ChatDataClient)[0];
  const window = { from: '2026-10-01T00:00:00Z', to: '2026-10-04T00:00:00Z', timezone: 'UTC' };
  const first = JSON.parse((await tool.handler(window)).content[0].text);
  expect(first.status).toBe('partial');
  expect(first.coverage).toEqual(expect.arrayContaining(missing));
  expect(first.last_key).toBe('page-21');
  expect(first.answer).not.toContain('No agent invocations were recorded');
  expect(runRequest).toHaveBeenCalledTimes(20);
  const resumed = JSON.parse((await tool.handler({ ...window, last_key: first.last_key })).content[0].text);
  expect(runRequest).toHaveBeenNthCalledWith(21, 'activity/work', { ...window, last_key: 'page-21' });
  expect(resumed.runs).toEqual([]);
  expect(resumed.last_key).toBeNull();
  expect(resumed.status).toBe('partial');
  expect(resumed.coverage.filter((entry: { source: string }) => entry.source === 'pagination')).toEqual([continuation]);
  expect(resumed.answer).toContain('earlier pages and their coverage are not included');
  expect(resumed.answer).not.toContain('No agent invocations were recorded');
});

it('can establish an empty window when it aggregates every page with all sources available', async () => {
  const available = ['activity_direct', 'activity_descendants', 'tasks']
    .map(source => ({ source, status: 'available', reason: 'queried' }));
  const runRequest = jest.fn()
    .mockResolvedValueOnce({ runs: [], issues: [], last_key: 'page-two', coverage: available })
    .mockResolvedValueOnce({
      runs: [], issues: [], last_key: null,
      coverage: [...available, { source: 'pagination', status: 'partial', reason: 'continuation_only' }],
    });
  const tool = activityTools({ runRequest } as unknown as ChatDataClient)[0];
  const result = JSON.parse((await tool.handler({ from: '2026-10-01T00:00:00Z', to: '2026-10-04T00:00:00Z', timezone: 'UTC' })).content[0].text);
  expect(result.status).toBe('empty');
  expect(result.coverage.every((entry: { status: string }) => entry.status === 'available')).toBe(true);
  expect(result.answer).toContain('No agent invocations were recorded');
});
