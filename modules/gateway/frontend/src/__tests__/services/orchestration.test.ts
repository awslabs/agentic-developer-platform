/**
 * Tests for the orchestration read API client — issue #4212.
 *
 * Separate from `GraphView.test.tsx`, which mocks this module: mocking it there
 * is right (the page's tests are about rendering, not transport) but it leaves the
 * one thing this file actually does — building the URL — unexercised. The path
 * prefix is the exact detail that broke #4330, so it gets a real assertion.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@/services/api', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
  buildQueryString: (params: Record<string, unknown>) => {
    const query = new URLSearchParams(Object.entries(params)
      .filter(([, value]) => value !== undefined && value !== null && value !== '')
      .map(([key, value]) => [key, String(value)]));
    return query.size ? `?${query}` : '';
  },
}));

import { startFlowIntake, getIntakeSession, sendIntakeTurn, deriveIntakePlan, listFlows, getFlowGraph, getFlowActivity, getGatePlanPreview, approveGate, rejectGate, acceptAmendment, editAmendment, previewAmendment, bindDraftProduct } from '@/services/orchestration';
import { apiClient } from '@/services/api';

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  mockGet.mockResolvedValue({ flow_id: 'flow-1', nodes: [], edges: [] });
});

describe('Product planning session continuation', () => {
  it('sends the same opening request and token through the server-owned idempotent intake route', async () => {
    const input = { product_id: 'product-a', repository: 'planning/epics', issue: '21', message: 'Build worker', retry_token: 'attempt-1' };
    await startFlowIntake(input);
    expect(apiClient.post).toHaveBeenCalledWith('/orchestration/intake/sessions', input);
  });

  it('reads the session, sends one tokened reply, and derives a read-only proposal at the scoped endpoints', async () => {
    await getIntakeSession('sess/1');
    expect(mockGet).toHaveBeenCalledWith('/orchestration/intake/sessions/sess%2F1');
    await sendIntakeTurn('sess/1', { message: 'delivery/worker', retry_token: 'answer-1' });
    expect(apiClient.post).toHaveBeenCalledWith('/orchestration/intake/sessions/sess%2F1/turns', { message: 'delivery/worker', retry_token: 'answer-1' });
    await deriveIntakePlan('sess/1');
    expect(apiClient.post).toHaveBeenCalledWith('/orchestration/intake/sessions/sess%2F1/plan', {});
  });
});

describe('getFlowGraph', () => {
  it('requests /orchestration/flows/<id> with no /api prefix', async () => {
    // `apiClient` prepends VITE_API_URL (`/api`), and CloudFront strips that
    // prefix before the ALB. Spelling it here yields /api/api/... which misses
    // the SPA fallback and returns HTML with a 200 rather than an error.
    await getFlowGraph('flow-abc');

    expect(mockGet).toHaveBeenCalledWith('/orchestration/flows/flow-abc?include_activity=false', undefined);
  });

  it('encodes the flow id so a hostile id cannot alter the path', async () => {
    await getFlowGraph('../admin/secrets');

    expect(mockGet).toHaveBeenCalledWith('/orchestration/flows/..%2Fadmin%2Fsecrets?include_activity=false', undefined);
  });

  it.each([
    { flow_id: 'flow-1', slug: 'legacy', nodes: [{ id: 'story', issue_ref: '42', issue_url: 'https://github.com/legacy/repo/issues/42' }], edges: [] },
    { flow_id: 'flow-1', slug: 'partial', product_status: 'unavailable', nodes: [{ id: 'story', repository_status: 'unbound' }], edges: [] },
  ])('passes an older or partially projected graph through without inventing additive fields', async (payload) => {
    mockGet.mockResolvedValue(payload);
    const controller = new AbortController();
    await expect(getFlowGraph('flow-1', controller.signal)).resolves.toBe(payload);
    expect(mockGet).toHaveBeenCalledWith('/orchestration/flows/flow-1?include_activity=false', controller.signal);
    expect(payload).not.toHaveProperty('product_id');
    expect(payload).not.toHaveProperty('planning_epics');
  });

  it('returns the payload unchanged', async () => {
    const payload = { flow_id: 'flow-1', slug: 'x', nodes: [], edges: [] };
    mockGet.mockResolvedValue(payload);

    await expect(getFlowGraph('flow-1')).resolves.toBe(payload);
  });
});

it('requests a bounded activity batch with repeated node_ids and a forwarded abort signal', async () => {
  const controller = new AbortController();
  await getFlowActivity('flow-1', ['story-a', 'story-b'], controller.signal);
  expect(mockGet).toHaveBeenCalledWith(
    '/orchestration/flows/flow-1/activity?node_ids=story-a&node_ids=story-b', controller.signal, true,
  );
});


describe('gate revision contract', () => {
  const plan = {version: 1, plan_hash: 'a'.repeat(64), superseded_at: null,
    plan_document: {title: 'Repair', nodes: [{address: 'f/e/w/g', title: 'Gate', kind: 'gate'}]}};
  it('saves a Product against the reviewed version without submitting or approving a plan', async () => {
    await bindDraftProduct('flow/1', 'product-1', plan);
    expect(apiClient.post).toHaveBeenCalledExactlyOnceWith('/orchestration/flows/flow%2F1/draft/product', {
      product_id: 'product-1', expected_plan_version: 1, expected_plan_hash: plan.plan_hash,
    });
    expect(apiClient.get).not.toHaveBeenCalled();
  });
  it.each([approveGate, rejectGate])('sends the reviewed hash without actor fields', async decide => {
    await decide('gate-1', 'Reviewed', plan.plan_hash);
    expect(apiClient.post).toHaveBeenCalledWith(expect.stringMatching(/gates\/gate-1\/(approve|reject)$/),
      {reason: 'Reviewed', expected_plan_hash: plan.plan_hash});
  });
  it('binds evaluation approval to the evidence in the reviewed preview', async () => {
    await approveGate('eval-1', 'Reviewed evidence', plan.plan_hash, {
      required: false, ready: true, problems: [], runs: [], evidence_id: 'gate-evidence-1',
    });
    expect(apiClient.post).toHaveBeenCalledWith('/orchestration/gates/eval-1/approve', {
      reason: 'Reviewed evidence', expected_plan_hash: plan.plan_hash,
      execution_preview: {snapshot: undefined, window_request: undefined, evidence_id: 'gate-evidence-1'},
    });
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

describe('amendment revision contract', () => {
  it('approves only the supplied draft hash and base without fetching an implicit latest revision', async () => {
    const draft = { draft_id: 'draft-1', proposal_hash: 'b'.repeat(64), base_plan_version: 2, base_plan_hash: 'a'.repeat(64) };
    await acceptAmendment('flow-1', draft, 'Reviewed dependencies');

    expect(apiClient.post).toHaveBeenCalledExactlyOnceWith('/orchestration/flows/flow-1/amendments/drafts/draft-1/accept', {
      expected_proposal_hash: draft.proposal_hash,
      expected_base_plan_version: 2,
      expected_base_plan_hash: draft.base_plan_hash,
      reason: 'Reviewed dependencies',
    });
    expect(apiClient.get).not.toHaveBeenCalled();
  });

  it('sends the edited graph intact and returns the server-issued revision without applying it', async () => {
    const document = { nodes: [{ address: 'flow/epic/wave/new', kind: 'eval' }], edges: [{ from_address: 'flow/epic/wave/checker', to_address: 'flow/epic/wave/new' }] };
    const revision = { draft_id: 'draft-2', proposal_hash: 'c'.repeat(64), superseded_draft_id: 'draft-1', unchanged: false };
    vi.mocked(apiClient.post).mockResolvedValue(revision);

    await expect(editAmendment('flow/1', 'draft/1', document, 'Add validation')).resolves.toBe(revision);
    expect(apiClient.post).toHaveBeenCalledExactlyOnceWith('/orchestration/flows/flow%2F1/amendments/drafts/draft%2F1/edit', {
      proposal: document, reason: 'Add validation',
    });
  });

  it('returns complete preview topology and blockers unchanged without accepting', async () => {
    const preview = {
      before_nodes: [{ address: 'flow/epic/wave/old' }], after_nodes: [{ address: 'flow/epic/wave/new' }],
      before_edges: [], after_edges: [], wave_changes: [{ wave: 'epic/wave' }],
      downstream: [{ address: 'flow/epic/wave/gate', newly_blocked: true }], gates_removed: ['flow/epic/wave/gate'],
      blocking: [{ rule: 'incoming_edges_changed', message: 'Keep completed work unchanged' }], acceptable: false,
    };
    vi.mocked(apiClient.post).mockResolvedValue(preview);

    await expect(previewAmendment('flow-1', 'draft-1')).resolves.toBe(preview);
    expect(apiClient.post).toHaveBeenCalledExactlyOnceWith('/orchestration/flows/flow-1/amendments/drafts/draft-1/preview', {});
  });
});

describe('listFlows archive visibility', () => {
  it('sends Product scope together with paging and archive filters', async () => {
    mockGet.mockResolvedValue({ product_id: 'product-a', flows: [], total: 0 });
    await listFlows({ product_id: 'product-a', limit: 25, offset: 25, include_archived: true });
    expect(mockGet).toHaveBeenLastCalledWith('/orchestration/flows?product_id=product-a&limit=25&offset=25&include_archived=true', undefined);
  });

  it.each([undefined, null, 'product-b'])('rejects an unconfirmed Product scope from an older or mismatched server: %s', async (productId) => {
    mockGet.mockResolvedValue({ product_id: productId, flows: [], total: 0 });
    await expect(listFlows({ product_id: 'product-a' })).rejects.toThrow('could not confirm this product');
  });

  it('defaults to live flows and requests archived flows only when opted in', async () => {
    await listFlows();
    expect(mockGet).toHaveBeenCalledWith('/orchestration/flows', undefined);

    await listFlows({ include_archived: true, status: 'archived' });
    expect(mockGet).toHaveBeenLastCalledWith('/orchestration/flows?status=archived&include_archived=true', undefined);
  });

  it('forwards cancellation for list requests', async () => {
    const controller = new AbortController();
    await listFlows({ limit: 25 }, controller.signal);
    expect(mockGet).toHaveBeenLastCalledWith('/orchestration/flows?limit=25', controller.signal);
  });
});
