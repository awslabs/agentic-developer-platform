/**
 * Tests for the orchestration read API client — issue #4212.
 *
 * Separate from `GraphView.test.tsx`, which mocks this module: mocking it there
 * is right (the page's tests are about rendering, not transport) but it leaves the
 * one thing this file actually does — building the URL — unexercised. The path
 * prefix is the exact detail that broke #4330, so it gets a real assertion.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn() } }));

import { getFlowGraph } from '@/services/orchestration';
import { apiClient } from '@/services/api';

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  mockGet.mockResolvedValue({ flow_id: 'flow-1', nodes: [], edges: [] });
});

describe('getFlowGraph', () => {
  it('requests /orchestration/flows/<id> with no /api prefix', async () => {
    // `apiClient` prepends VITE_API_URL (`/api`), and CloudFront strips that
    // prefix before the ALB. Spelling it here yields /api/api/... which misses
    // the SPA fallback and returns HTML with a 200 rather than an error.
    await getFlowGraph('flow-abc');

    expect(mockGet).toHaveBeenCalledWith('/orchestration/flows/flow-abc');
  });

  it('encodes the flow id so a hostile id cannot alter the path', async () => {
    await getFlowGraph('../admin/secrets');

    expect(mockGet).toHaveBeenCalledWith('/orchestration/flows/..%2Fadmin%2Fsecrets');
  });

  it('returns the payload unchanged', async () => {
    const payload = { flow_id: 'flow-1', slug: 'x', nodes: [], edges: [] };
    mockGet.mockResolvedValue(payload);

    await expect(getFlowGraph('flow-1')).resolves.toBe(payload);
  });
});
