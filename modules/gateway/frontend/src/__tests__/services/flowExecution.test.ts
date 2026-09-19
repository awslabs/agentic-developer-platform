/**
 * Transport tests for the execution read client — issue #5145.
 *
 * Separate from the component tests, which mock this module: mocking it there is
 * right (those tests are about rendering) but it leaves the one thing this file
 * does — building the URL and threading the abort signal — unexercised. The path
 * prefix is the exact detail that broke #4330, so it gets a real assertion, and so
 * does the signal, because a polling read that cannot be cancelled is a leak the
 * type checker is perfectly happy with.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/services/api', () => ({
  apiClient: { get: vi.fn() },
  buildQueryString: (params: Record<string, unknown>) => {
    // The real helper drops undefined/null/'' — reproduced rather than imported so
    // an assertion about an omitted parameter is an assertion about this contract.
    const entries = Object.entries(params).filter(
      ([, value]) => value !== undefined && value !== null && value !== ''
    );
    return entries.length ? `?${entries.map(([k, v]) => `${k}=${String(v)}`).join('&')}` : '';
  },
}));

import { getFlowExecution } from '@/services/orchestration';
import { apiClient } from '@/services/api';

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  mockGet.mockResolvedValue({
    flow_id: 'flow-1',
    server_time: '2026-09-18T12:00:00+00:00',
    executions: [],
    total: 0,
    limit: 200,
    offset: 0,
    legacy: true,
  });
});

describe('getFlowExecution', () => {
  it('requests /orchestration/... with no /api prefix', async () => {
    // `apiClient` prepends VITE_API_URL (`/api`), and CloudFront's viewer function
    // strips that prefix before the ALB. Spelling it here yields
    // /api/api/orchestration/..., which misses the SPA fallback and returns HTML
    // with a 200 — a failure that looks like corrupt data, not a bad URL (#4330).
    await getFlowExecution('flow-abc');

    expect(mockGet).toHaveBeenCalledWith('/orchestration/flows/flow-abc/execution', undefined);
  });

  it('encodes the flow id so a hostile id cannot alter the path', async () => {
    await getFlowExecution('../admin/secrets');

    expect(mockGet).toHaveBeenCalledWith(
      '/orchestration/flows/..%2Fadmin%2Fsecrets/execution',
      undefined
    );
  });

  it('omits paging parameters entirely when unset', async () => {
    // Not `?limit=&offset=`: the route 422s on an empty value, and a default that
    // travels in the URL is a default the server cannot change.
    await getFlowExecution('flow-1');

    expect(mockGet).toHaveBeenCalledWith('/orchestration/flows/flow-1/execution', undefined);
  });

  it('sends paging parameters when given', async () => {
    await getFlowExecution('flow-1', { limit: 50, offset: 100 });

    expect(mockGet).toHaveBeenCalledWith(
      '/orchestration/flows/flow-1/execution?limit=50&offset=100',
      undefined
    );
  });

  it('forwards the abort signal so an in-flight poll can be cancelled', async () => {
    // The mechanism behind cancel-on-navigation. Without the signal reaching the
    // transport, unmounting the page leaves requests running against a screen
    // nobody is looking at — and nothing about the types would object.
    const controller = new AbortController();

    await getFlowExecution('flow-1', {}, controller.signal);

    expect(mockGet).toHaveBeenCalledWith(
      '/orchestration/flows/flow-1/execution',
      controller.signal
    );
  });

  it('returns the payload unchanged', async () => {
    const payload = {
      flow_id: 'flow-1',
      server_time: '2026-09-18T12:00:00+00:00',
      executions: [],
      total: 0,
      limit: 200,
      offset: 0,
      legacy: false,
    };
    mockGet.mockResolvedValue(payload);

    await expect(getFlowExecution('flow-1')).resolves.toBe(payload);
  });
});
