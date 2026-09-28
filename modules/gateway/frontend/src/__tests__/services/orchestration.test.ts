/**
 * Tests for the orchestration read API client — issue #4212.
 *
 * Separate from `GraphView.test.tsx`, which mocks this module: mocking it there
 * is right (the page's tests are about rendering, not transport) but it leaves the
 * one thing this file actually does — building the URL — unexercised. The path
 * prefix is the exact detail that broke #4330, so it gets a real assertion.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));

import { getFlowGraph, getGatePlanPreview, approveGate, rejectGate } from '@/services/orchestration';
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


describe('gate revision contract', () => {
  const plan = {version: 1, plan_hash: 'a'.repeat(64), superseded_at: null,
    plan_document: {title: 'Repair', nodes: [{address: 'f/e/w/g', title: 'Gate', kind: 'gate'}]}};
  it.each([approveGate, rejectGate])('sends the reviewed hash without actor fields', async decide => {
    await decide('gate-1', 'Reviewed', plan.plan_hash);
    expect(apiClient.post).toHaveBeenCalledWith(expect.stringMatching(/gates\/gate-1\/(approve|reject)$/),
      {reason: 'Reviewed', expected_plan_hash: plan.plan_hash});
  });
  it('selects the current revision rather than a superseded plan', async () => {
    mockGet.mockResolvedValue([{...plan, superseded_at: '2026-09-27'}, plan]);
    await expect(getGatePlanPreview('flow-1')).resolves.toEqual(plan);
    expect(mockGet).toHaveBeenCalledWith('/orchestration/flows/flow-1/plans');
  });
  it.each([[], [plan, plan], [{...plan, plan_hash: ''}], [{...plan, plan_document: {nodes: [null]}}]])(
    'refuses missing, ambiguous or malformed revisions: %j', async plans => {
      mockGet.mockResolvedValue(plans);
      await expect(getGatePlanPreview('flow-1')).rejects.toThrow('could not be verified');
    });
});
