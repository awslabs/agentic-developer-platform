import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AmendmentReviewPanel } from '@/components/orchestration/AmendmentReviewPanel';
import type { AmendmentDraft, AmendmentImpact, FlowGraph } from '@/types/orchestration';

vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({ hasPermission: () => true }) }));
vi.mock('@/services/orchestration', () => ({
  listAmendments: vi.fn(),
  getAmendment: vi.fn(),
  previewAmendment: vi.fn(),
  proposeAmendment: vi.fn(),
  editAmendment: vi.fn(),
  rejectAmendment: vi.fn(),
  acceptAmendment: vi.fn(),
}));

import * as service from '@/services/orchestration';

const flow: FlowGraph = {
  flow_id: 'flow-1', slug: 'delivery', title: 'Delivery', intent_ref: '1', state: 'running', edges: [],
  cost: { status: 'unknown', amount_usd: null, reason: 'none', partial: true, scope: 'test' },
  nodes: [{
    id: 'node-1', epic_ref: 'epic-1', wave_ref: 'wave-1', node_ref: 'story-1', kind: 'story', title: 'Story',
    state: 'passed', stalled: false, issue_ref: '1', attempts: 1,
    cost: { status: 'unknown', amount_usd: null, reason: 'none', scope: 'test' },
  }],
};

const impact: AmendmentImpact = {
  flow_id: 'flow-1', base_plan_version: 2, base_plan_hash: 'a'.repeat(64), acceptable: true, is_noop: false,
  nodes: [
    { address: 'delivery/epic-1/wave-1/story-1', change: 'unchanged', kind: 'story', title: 'Story', attempts: 1 },
    { address: 'delivery/epic-1/wave-1/new', change: 'added', kind: 'story', title: 'New', attempts: 0 },
  ],
  edges: [{ from_address: 'delivery/epic-1/wave-1/story-1', to_address: 'delivery/epic-1/wave-1/new', change: 'added' }],
  before_nodes: [{ address: 'delivery/epic-1/wave-1/story-1', kind: 'story', title: 'Story' }],
  after_nodes: [
    { address: 'delivery/epic-1/wave-1/story-1', kind: 'story', title: 'Story' },
    { address: 'delivery/epic-1/wave-1/new', kind: 'story', title: 'New' },
  ],
  before_edges: [],
  after_edges: [{ from_address: 'delivery/epic-1/wave-1/story-1', to_address: 'delivery/epic-1/wave-1/new' }],
  wave_changes: [], execution_order: [{ stage: 1, waves: ['epic-1/wave-1'] }], downstream: [],
  gates_added: [], gates_removed: [], policy_before: null, policy_after: null, policy_changed: false, blocking: [],
};

const reviewedDraft: AmendmentDraft = {
  draft_id: 'draft-1', flow_id: 'flow-1', state: 'pending', proposal_hash: 'b'.repeat(64),
  base_plan_version: 2, base_plan_hash: 'a'.repeat(64), base_is_current: true,
  proposal_document: { flow_slug: 'delivery', nodes: impact.after_nodes, edges: impact.after_edges },
};

function renderPanel(client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }), currentFlow: FlowGraph = flow) {
  return render(<QueryClientProvider client={client}><AmendmentReviewPanel flow={currentFlow} /></QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(service.listAmendments).mockResolvedValue([]);
});

describe('AmendmentReviewPanel', () => {
  it('shows saved file groups inside the proposed graph nodes', async () => {
    const afterNodes = impact.after_nodes!.map(node => ({ ...node, file_groups: [{
      group: 'Configuration', create: ['src/config.py'], modify: [], modify_after: [],
    }] }));
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft]);
    vi.mocked(service.getAmendment).mockResolvedValue(reviewedDraft);
    vi.mocked(service.previewAmendment).mockResolvedValue({ ...impact, after_nodes: afterNodes });
    renderPanel();
    await screen.findByRole('option', { name: /draft-1/ });
    fireEvent.change(screen.getByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    const graph = await screen.findByTestId('amendment-graph-after');
    expect(within(graph).getAllByText('Planned files')).toHaveLength(2);
    await userEvent.click(within(graph).getAllByText('Planned files')[0]);
    expect(within(graph).getAllByText('src/config.py')[0]).toBeVisible();
    expect(within(screen.getByTestId('amendment-graph-before')).queryByText('Planned files')).not.toBeInTheDocument();
  });

  it.each([false, true])('warns about dropped receipts after acceptance (replayed=%s)', async (alreadyAccepted) => {
    const dropped = [
      { kind: 'budget' as const, decision_id: 'budget-1',
        before: { max_spend_usd: '50.00', max_run_spend_usd: '75.00', max_chain_spend_usd: '75.00' },
        after: { max_spend_usd: '50.00', max_run_spend_usd: '25.00', max_chain_spend_usd: '25.00' }, reason: 'base limits changed' },
      { kind: 'window' as const, decision_id: 'window-1',
        before: { expires_at: '2030-01-01T02:00:00+00:00', max_wall_clock_seconds: 7200 },
        after: { expires_at: '2030-01-01T01:00:00+00:00', max_wall_clock_seconds: 3600 }, reason: 'base limits changed' },
      { kind: 'retry' as const, decision_id: 'retry-1', before: 5, after: 3, reason: 'base limits changed' },
      { kind: 'concurrency' as const, decision_id: 'concurrency-1', before: 7, after: 2, reason: 'base limits changed' },
    ];
    const limits = { max_spend_usd: '50.00', max_attempts_per_node: 5, max_concurrent_actions: 7,
      max_wall_clock_seconds: 7200, expires_at: '2030-01-01T02:00:00+00:00' };
    const afterLimits = { ...limits, max_attempts_per_node: 3, max_concurrent_actions: 2,
      max_wall_clock_seconds: 3600, expires_at: '2030-01-01T01:00:00+00:00' };
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft]);
    vi.mocked(service.getAmendment).mockResolvedValue(reviewedDraft);
    vi.mocked(service.previewAmendment).mockResolvedValue({
      ...impact, receipts_dropped: dropped, effective_limits_before: limits,
      effective_limits_after: afterLimits,
    });
    vi.mocked(service.acceptAmendment).mockResolvedValue({
      draft_id: 'draft-1', flow_id: flow.flow_id, plan_version: 3, decision_id: 'amendment-1',
      already_accepted: alreadyAccepted, receipts_dropped: dropped,
      effective_limits_before: limits, effective_limits_after: afterLimits,
    });
    renderPanel();
    await screen.findByRole('option', { name: /draft-1/ });
    fireEvent.change(screen.getByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    expect(await screen.findByTestId('amendment-effective-limits')).toHaveTextContent('max_spend_usd: 50.00 → 50.00');
    expect(screen.getByTestId('amendment-impact')).toHaveTextContent('Accepted increases would be dropped on accept');
    const budgetText = 'budget (decision budget-1): flow $50.00, run $75.00, chain $75.00 → flow $50.00, run $25.00, chain $25.00';
    const windowText = 'window (decision window-1): expiry 2030-01-01T02:00:00+00:00, wall-clock 7200 seconds → expiry 2030-01-01T01:00:00+00:00, wall-clock 3600 seconds';
    expect(screen.getByTestId('amendment-impact')).toHaveTextContent(budgetText);
    expect(screen.getByTestId('amendment-impact')).toHaveTextContent(windowText);
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Accept exact revision' }));
    const committed = await screen.findByRole('region', { name: 'Committed receipt outcome' });
    expect(committed).toHaveTextContent(`Accepted plan v3${alreadyAccepted ? ' (replayed)' : ''}`);
    expect(committed).toHaveTextContent(`${budgetText} — base limits changed`);
    expect(committed).toHaveTextContent(windowText);
    expect(committed).toHaveTextContent('retry (decision retry-1): 5 → 3');
    expect(committed).toHaveTextContent('concurrency (decision concurrency-1): 7 → 2');
    expect(committed).not.toHaveTextContent('[object Object]');
    expect(screen.getByTestId('amendment-committed-limits')).toHaveTextContent('max_spend_usd: 50.00 → 50.00');
  });

  it('saves edits and loads the new preview before accepting that exact revision', async () => {
    const gate = { address: 'delivery/epic-1/wave-2/acceptance', kind: 'gate', title: 'Review cleanup validation' };
    const edge = { from_address: 'delivery/epic-1/wave-1/new', to_address: gate.address };
    const revisedImpact: AmendmentImpact = {
      ...impact, after_nodes: [...impact.after_nodes ?? [], gate], after_edges: [...impact.after_edges ?? [], edge],
      nodes: [...impact.nodes, { ...gate, change: 'added', attempts: 0 }],
      edges: [...impact.edges, { ...edge, change: 'added' }], gates_added: [gate.address],
    };
    const revisedDocument = { ...reviewedDraft.proposal_document, nodes: revisedImpact.after_nodes, edges: revisedImpact.after_edges };
    const revisedDraft = { ...reviewedDraft, draft_id: 'draft-2', proposal_hash: 'c'.repeat(64), proposal_document: revisedDocument };
    let resolvePreview: (value: AmendmentImpact) => void = () => undefined;
    const pendingPreview = new Promise<AmendmentImpact>((resolve) => { resolvePreview = resolve; });
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft, revisedDraft]);
    vi.mocked(service.getAmendment).mockImplementation(async (_flowId, draftId) => draftId === 'draft-2' ? revisedDraft : reviewedDraft);
    vi.mocked(service.previewAmendment).mockImplementation(async (_flowId, draftId) => draftId === 'draft-2' ? pendingPreview : impact);
    vi.mocked(service.editAmendment).mockResolvedValue({ draft_id: 'draft-2', flow_id: flow.flow_id, proposal_hash: revisedDraft.proposal_hash, impact: revisedImpact });
    vi.mocked(service.acceptAmendment).mockResolvedValue({ draft_id: 'draft-2', flow_id: flow.flow_id, plan_version: 3, decision_id: 'decision-1', already_accepted: false });
    renderPanel();
    await screen.findByRole('option', { name: /draft-1/ });
    fireEvent.change(screen.getByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeEnabled());

    fireEvent.change(screen.getByLabelText('Proposal document'), { target: { value: JSON.stringify(revisedDocument) } });
    fireEvent.change(screen.getByLabelText('Decision reason'), { target: { value: 'Require validation first' } });
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeDisabled();
    expect(screen.getByText(/Save your edits/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save new revision' }));
    await waitFor(() => expect(service.editAmendment).toHaveBeenCalledWith(flow.flow_id, 'draft-1', revisedDocument, 'Require validation first'));
    await waitFor(() => expect(service.previewAmendment).toHaveBeenCalledWith(flow.flow_id, 'draft-2'));
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeDisabled();
    expect(service.acceptAmendment).not.toHaveBeenCalled();

    await act(async () => { resolvePreview(revisedImpact); });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeEnabled());
    expect(screen.getByTestId('amendment-graph-after')).toHaveTextContent(gate.title);
    expect(screen.getByTestId('amendment-graph-after')).toHaveTextContent(`${edge.from_address} → ${edge.to_address}`);
    fireEvent.click(screen.getByRole('button', { name: 'Accept exact revision' }));
    await waitFor(() => expect(service.acceptAmendment).toHaveBeenCalledWith(flow.flow_id, revisedDraft, 'Require validation first'));
  });

  it('preserves unsaved edits when revision metadata refreshes', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft]);
    vi.mocked(service.getAmendment).mockResolvedValue(reviewedDraft);
    vi.mocked(service.previewAmendment).mockResolvedValue(impact);
    renderPanel(client);
    await screen.findByRole('option', { name: /draft-1/ });
    fireEvent.change(screen.getByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    const editor = await screen.findByLabelText('Proposal document');
    const editedText = JSON.stringify({ ...reviewedDraft.proposal_document, description: 'Unsaved work' });
    fireEvent.change(editor, { target: { value: editedText } });
    vi.mocked(service.getAmendment).mockResolvedValue({ ...reviewedDraft, request_text: 'Refreshed metadata' });

    await act(async () => { await client.invalidateQueries({ queryKey: ['orchestration', 'amendment', flow.flow_id] }); });
    await waitFor(() => expect(client.getQueryData<AmendmentDraft>(['orchestration', 'amendment', flow.flow_id, 'draft-1'])?.request_text).toBe('Refreshed metadata'));
    expect(editor).toHaveValue(editedText);
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeDisabled();
  });

  it.each(['amendment', 'amendment-preview'])('does not approve cached data after %s readback fails', async (queryName) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft]);
    vi.mocked(service.getAmendment).mockResolvedValue(reviewedDraft);
    vi.mocked(service.previewAmendment).mockResolvedValue(impact);
    renderPanel(client);
    await screen.findByRole('option', { name: /draft-1/ });
    fireEvent.change(screen.getByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeEnabled());
    vi.mocked(queryName === 'amendment' ? service.getAmendment : service.previewAmendment).mockRejectedValue(new Error('Readback unavailable'));

    await act(async () => { await client.invalidateQueries({ queryKey: ['orchestration', queryName, flow.flow_id] }); });
    expect(await screen.findByText('Readback unavailable')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeDisabled();
    expect(service.acceptAmendment).not.toHaveBeenCalled();
  });

  it('clears a formatting-only edit after the server keeps the same revision', async () => {
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft]);
    vi.mocked(service.getAmendment).mockResolvedValue(reviewedDraft);
    vi.mocked(service.previewAmendment).mockResolvedValue(impact);
    vi.mocked(service.editAmendment).mockResolvedValue({ draft_id: 'draft-1', flow_id: flow.flow_id, proposal_hash: reviewedDraft.proposal_hash, impact });
    renderPanel();
    await screen.findByRole('option', { name: /draft-1/ });
    fireEvent.change(screen.getByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    const editor = await screen.findByLabelText('Proposal document');
    fireEvent.change(editor, { target: { value: JSON.stringify(reviewedDraft.proposal_document) } });
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save new revision' }));

    await waitFor(() => expect(editor).toHaveValue(JSON.stringify(reviewedDraft.proposal_document, null, 2)));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeEnabled());
    expect(service.acceptAmendment).not.toHaveBeenCalled();
  });

  it.each([{ base_plan_version: 3 }, { base_plan_hash: 'd'.repeat(64) }])('does not accept a preview for a different base: %j', async (changedBase) => {
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft]);
    vi.mocked(service.getAmendment).mockResolvedValue(reviewedDraft);
    vi.mocked(service.previewAmendment).mockResolvedValue({ ...impact, ...changedBase });
    renderPanel();
    await screen.findByRole('option', { name: /draft-1/ });
    fireEvent.change(screen.getByLabelText('Pending amendment'), { target: { value: 'draft-1' } });

    await screen.findByTestId('amendment-impact');
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeDisabled();
    expect(service.acceptAmendment).not.toHaveBeenCalled();
  });

  it('rejects the selected revision without accepting or saving unsaved edits', async () => {
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft]);
    vi.mocked(service.getAmendment).mockResolvedValue(reviewedDraft);
    vi.mocked(service.previewAmendment).mockResolvedValue(impact);
    vi.mocked(service.rejectAmendment).mockImplementation(async () => {
      const rejected = { ...reviewedDraft, state: 'rejected' };
      vi.mocked(service.getAmendment).mockResolvedValue(rejected);
      return rejected;
    });
    renderPanel();
    await screen.findByRole('option', { name: /draft-1/ });
    fireEvent.change(screen.getByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    fireEvent.change(await screen.findByLabelText('Proposal document'), { target: { value: 'unsaved edits' } });
    fireEvent.change(screen.getByLabelText('Decision reason'), { target: { value: 'Use a different placement' } });
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));

    await waitFor(() => expect(service.rejectAmendment).toHaveBeenCalledWith(flow.flow_id, 'draft-1', 'Use a different placement'));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled());
    expect(service.acceptAmendment).not.toHaveBeenCalled();
    expect(service.editAmendment).not.toHaveBeenCalled();
  });

  it.each(['insert_before', 'insert_after', 'parallel_branch', 'existing_wave', 'new_wave'])('submits explicit %s placement without applying it', async (placement) => {
    vi.mocked(service.proposeAmendment).mockResolvedValue({ draft_id: 'draft-1', flow_id: 'flow-1', proposal_hash: 'b'.repeat(64), base_plan_version: 2, impact });
    renderPanel();
    fireEvent.change(screen.getByLabelText('Request'), { target: { value: 'Add discovered work' } });
    fireEvent.change(screen.getByLabelText('Work title'), { target: { value: 'New work' } });
    fireEvent.change(screen.getByLabelText('Work reference'), { target: { value: 'new-work' } });
    fireEvent.change(screen.getByLabelText('Existing issue'), { target: { value: '5329' } });
    fireEvent.change(screen.getByLabelText('Story repository (owner/name)'), { target: { value: 'aws-e/adp' } });
    fireEvent.change(screen.getByLabelText('Criteria'), { target: { value: 'cleanup-complete' } });
    fireEvent.change(screen.getByLabelText('Required inputs'), { target: { value: 'live-account=environment:prod' } });
    fireEvent.change(screen.getByLabelText('Evidence expectations'), { target: { value: 'trusted-live-observation' } });
    fireEvent.change(screen.getByLabelText('Prerequisite addresses'), { target: { value: ' delivery/epic-1/wave-1/story-1, , delivery/epic-1/wave-1/inputs, ' } });
    fireEvent.change(screen.getByLabelText('Successor addresses'), { target: { value: ' delivery/epic-1/wave-3/acceptance, ' } });
    fireEvent.change(screen.getByLabelText('Placement'), { target: { value: placement } });
    if (placement === 'parallel_branch') fireEvent.change(screen.getByLabelText('Rejoin addresses'), { target: { value: 'delivery/epic-1/wave-1/story-1' } });
    if (placement === 'new_wave') fireEvent.change(screen.getByLabelText('Wave reference'), { target: { value: 'wave-2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Propose without applying' }));
    await waitFor(() => expect(service.proposeAmendment).toHaveBeenCalled());
    expect(vi.mocked(service.proposeAmendment).mock.calls[0][1]).toMatchObject({ placement: { placement: { kind: placement } } });
    expect(vi.mocked(service.proposeAmendment).mock.calls[0][1]).toMatchObject({ placement: { work: [{ issue_ref: '5329', repository_identity: 'aws-e/adp', criteria: ['cleanup-complete'], required_inputs: [{ name: 'live-account', source: 'environment:prod' }], evidence_expectations: ['trusted-live-observation'] }] } });
    expect(vi.mocked(service.proposeAmendment).mock.calls[0][1]).toMatchObject({ placement: { work: [{
      requires: ['delivery/epic-1/wave-1/story-1', 'delivery/epic-1/wave-1/inputs'],
      precedes: ['delivery/epic-1/wave-3/acceptance'],
    }] } });
    expect(service.acceptAmendment).not.toHaveBeenCalled();
  });

  it('leaves optional dependencies empty rather than inventing placement edges', async () => {
    vi.mocked(service.proposeAmendment).mockResolvedValue({ draft_id: 'draft-1', flow_id: 'flow-1', proposal_hash: 'b'.repeat(64), base_plan_version: 2, impact });
    renderPanel();
    fireEvent.change(screen.getByLabelText('Request'), { target: { value: 'Add discovered work' } });
    fireEvent.change(screen.getByLabelText('Work title'), { target: { value: 'New work' } });
    fireEvent.change(screen.getByLabelText('Work reference'), { target: { value: 'new-work' } });
    fireEvent.change(screen.getByLabelText('Existing issue'), { target: { value: '5329' } });
    fireEvent.click(screen.getByRole('button', { name: 'Propose without applying' }));

    await waitFor(() => expect(service.proposeAmendment).toHaveBeenCalled());
    expect(vi.mocked(service.proposeAmendment).mock.calls[0][1]).toMatchObject({ placement: {
      work: [{ requires: [], precedes: [] }],
      placement: { kind: 'insert_after', anchor: 'delivery/epic-1/wave-1/story-1' },
    } });
    expect(service.acceptAmendment).not.toHaveBeenCalled();
  });

  it('shows server dependency conflicts without applying the proposed work', async () => {
    vi.mocked(service.proposeAmendment).mockRejectedValue(new Error('placement_prerequisite_unknown: Choose an existing prerequisite address.'));
    renderPanel();
    fireEvent.change(screen.getByLabelText('Request'), { target: { value: 'Add validation' } });
    fireEvent.change(screen.getByLabelText('Work title'), { target: { value: 'Cleanup validation' } });
    fireEvent.change(screen.getByLabelText('Work reference'), { target: { value: 'cleanup-validation' } });
    fireEvent.change(screen.getByLabelText('Existing issue'), { target: { value: '5330' } });
    fireEvent.change(screen.getByLabelText('Work kind'), { target: { value: 'eval' } });
    fireEvent.change(screen.getByLabelText('Placement'), { target: { value: 'new_wave' } });
    fireEvent.change(screen.getByLabelText('Wave reference'), { target: { value: 'validation' } });
    fireEvent.change(screen.getByLabelText('Prerequisite addresses'), { target: { value: 'delivery/epic-1/wave-1/missing' } });
    fireEvent.change(screen.getByLabelText('Successor addresses'), { target: { value: 'delivery/epic-1/wave-3/acceptance' } });
    fireEvent.click(screen.getByRole('button', { name: 'Propose without applying' }));

    expect(await screen.findByText(/placement_prerequisite_unknown/)).toBeInTheDocument();
    expect(screen.getByLabelText('Prerequisite addresses')).toHaveValue('delivery/epic-1/wave-1/missing');
    expect(service.acceptAmendment).not.toHaveBeenCalled();
  });

  it('renders complete before and after topology including unchanged context', async () => {
    const draft = { draft_id: 'draft-1', flow_id: 'flow-1', state: 'pending', proposal_hash: 'b'.repeat(64), base_plan_version: 2, base_plan_hash: 'a'.repeat(64), base_is_current: true, proposal_document: { flow_slug: 'delivery' } };
    vi.mocked(service.listAmendments).mockResolvedValue([draft]);
    vi.mocked(service.getAmendment).mockResolvedValue(draft);
    vi.mocked(service.previewAmendment).mockResolvedValue(impact);
    renderPanel();
    fireEvent.change(await screen.findByLabelText('Pending amendment'), { target: { value: 'draft-1' } });

    expect(await screen.findByTestId('amendment-graph-before')).toHaveTextContent('Story · unchanged');
    expect(screen.getByTestId('amendment-graph-after')).toHaveTextContent('Story · unchanged');
    expect(screen.getByTestId('amendment-graph-after')).toHaveTextContent('story-1 → delivery/epic-1/wave-1/new');
  });

  it('shows blockers and authority changes and disables acceptance of a stale revision', async () => {
    const blocked = { ...impact, acceptable: false, policy_changed: true, policy_after: { autonomous_actions: ['develop'] }, blocking: [{ rule: 'stale_base', message: 'Refresh the proposal.' }] } as AmendmentImpact;
    const draft = { draft_id: 'draft-1', flow_id: 'flow-1', state: 'pending', proposal_hash: 'b'.repeat(64), base_plan_version: 2, base_plan_hash: 'a'.repeat(64), base_is_current: false, proposal_document: { flow_slug: 'delivery' } };
    vi.mocked(service.listAmendments).mockResolvedValue([draft]);
    vi.mocked(service.getAmendment).mockResolvedValue(draft);
    vi.mocked(service.previewAmendment).mockResolvedValue(blocked);
    renderPanel();
    await screen.findByRole('option', { name: /draft-1/ });
    fireEvent.change(screen.getByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    expect(await screen.findByText(/stale_base/)).toBeInTheDocument();
    expect(screen.getByText('Execution authority changes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeDisabled();
  });

  it('shows effective limits before and after acceptance', async () => {
    const limits = { max_attempts_per_node: 3, max_concurrent_actions: 2, max_wall_clock_seconds: 3600, expires_at: '2026-10-08T00:00:00+00:00' };
    const lowering = {
      ...impact, acceptable: false,
      effective_limits_before: { ...limits, max_spend_usd: '500.00' },
      effective_limits_after: { ...limits, max_spend_usd: '200.00' },
      blocking: [{ rule: 'effective_limit_decrease', message: 'State the limits explicitly.', where: 'execution_policy.limits.max_spend_usd' }],
    } as AmendmentImpact;
    const draft = { draft_id: 'draft-1', flow_id: 'flow-1', state: 'pending', proposal_hash: 'b'.repeat(64), base_plan_version: 2, base_plan_hash: 'a'.repeat(64), base_is_current: true, proposal_document: { flow_slug: 'delivery' } };
    vi.mocked(service.listAmendments).mockResolvedValue([draft]);
    vi.mocked(service.getAmendment).mockResolvedValue(draft);
    vi.mocked(service.previewAmendment).mockResolvedValue(lowering);
    renderPanel();
    fireEvent.change(await screen.findByLabelText('Pending amendment'), { target: { value: 'draft-1' } });

    const section = await screen.findByTestId('amendment-effective-limits');
    expect(section).toHaveTextContent('max_spend_usd: 500.00 → 200.00 (changed)');
    expect(section).toHaveTextContent('max_attempts_per_node: 3 → 3');
    expect(screen.getByText(/effective_limit_decrease/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeDisabled();
  });

  it('compares accepted and proposed repositories before accepting an untouched story change', async () => {
    const changed = {
      ...impact,
      nodes: [{ address: 'delivery/epic-1/wave-1/story-1', change: 'modified', kind: 'story', title: 'Checkout API', state: 'pending', attempts: 0,
        before: { repository_identity: 'delivery/api', provider_repository_id: 11, repository_connection: { provider: 'github', installation_id: 3 } },
        after: { repository_identity: 'delivery/web', provider_repository_id: 12, repository_connection: { provider: 'github', installation_id: 4 } },
      }],
    } as AmendmentImpact;
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft]);
    vi.mocked(service.getAmendment).mockResolvedValue(reviewedDraft);
    vi.mocked(service.previewAmendment).mockResolvedValue(changed);
    renderPanel();
    fireEvent.change(await screen.findByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    const scope = await screen.findByRole('region', { name: 'Repository scope change for Checkout API' });
    expect(scope).toHaveTextContent('Accepted target: delivery/api (provider ID 11, installation 3)');
    expect(scope).toHaveTextContent('Proposed target: delivery/web (provider ID 12, installation 4)');
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeEnabled();
    expect(screen.getByLabelText('Story repository (owner/name)')).toHaveAttribute('placeholder', 'owner/repo');
    expect(screen.queryByLabelText('Project')).not.toBeInTheDocument();
  });

  it.each([false, true])('shows server preview identities separately from Product and epic (started=%s)', async (started) => {
    const address = 'delivery/epic-1/wave-1/story-1';
    const before = { address, kind: 'story', title: 'Checkout API', issue_ref: '#42',
      proposal_identity: 'proposal:accepted', repository_identity: 'delivery/api', provider_repository_id: 11,
      repository_connection: { provider: 'github', installation_id: 3 } };
    const after = { ...before, issue_ref: '#81', proposal_identity: 'proposal:proposed',
      repository_identity: 'delivery/web', provider_repository_id: 12,
      repository_connection: { provider: 'github', installation_id: 4 } };
    const added = { address: 'delivery/epic-1/wave-1/new', kind: 'story', title: 'Worker',
      issue_ref: '#91', proposal_identity: 'proposal:new', repository_identity: 'delivery/worker',
      provider_repository_id: 13, repository_connection: { provider: 'github', installation_id: 5 } };
    const preview: AmendmentImpact = { ...impact, product_before: 'product-a', product_after: 'product-b',
      before_nodes: [before], after_nodes: [after, added], acceptable: !started,
      blocking: started ? [{ rule: 'amendment_definition_change', message: 'Cannot retarget a started story.', where: address }] : [],
      nodes: [{ address, change: 'modified', kind: 'story', title: 'Checkout API', state: started ? 'running' : 'pending',
        attempts: started ? 1 : 0, before, after },
        { address: added.address, change: 'added', kind: 'story', title: 'Worker', attempts: 0, after: added }],
    };
    const draft = { ...reviewedDraft, proposal_document: { flow_slug: 'delivery', nodes: [after, added], edges: [] } };
    vi.mocked(service.listAmendments).mockResolvedValue([draft]);
    vi.mocked(service.getAmendment).mockResolvedValue(draft);
    vi.mocked(service.previewAmendment).mockResolvedValue(preview);
    renderPanel(undefined, { ...flow, product_id: 'product-a', product_name: 'Checkout', product_status: 'accessible',
      planning_epics: [{ scope: 'epic', address: 'delivery/epic-1', issue: {
        provider: 'github', repository_id: 30, repository: 'planning/epics', installation_id: 3, issue_number: 21,
      } }],
      nodes: [{ ...flow.nodes[0], repository_identity: 'not/the/accepted/repo' }],
    });
    fireEvent.change(await screen.findByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    const scope = await screen.findByRole('region', { name: 'Repository scope change for Checkout API' });
    expect(screen.getByRole('region', { name: 'Product scope in amendment' })).toHaveTextContent('Current Product: Checkout (product-a)');
    expect(screen.getByRole('region', { name: 'Product scope in amendment' })).toHaveTextContent('Proposed Product: product-b (access not verified)');
    expect(screen.getByText('Current planning epic: planning/epics#21')).toBeVisible();
    expect(scope).toHaveTextContent('Accepted target: delivery/api (provider ID 11, installation 3) · issue #42 · proposal proposal:accepted');
    expect(scope).toHaveTextContent('Proposed target: delivery/web (provider ID 12, installation 4) · issue #81 · proposal proposal:proposed');
    expect(scope).not.toHaveTextContent('not/the/accepted/repo');
    expect(within(screen.getByRole('list', { name: 'Before story targets' })).getByText(/Checkout API: delivery\/api.*issue #42/)).toBeVisible();
    expect(screen.getByRole('list', { name: 'Before story targets' })).not.toHaveTextContent('Worker');
    expect(within(screen.getByRole('list', { name: 'After story targets' })).getByText(/Worker: delivery\/worker.*issue #91.*proposal proposal:new/)).toBeVisible();
    expect(screen.getByRole('list', { name: 'After story targets' }).querySelector('a')).toBeNull();
    expect(service.previewAmendment).toHaveBeenCalledWith(flow.flow_id, draft.draft_id);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept exact revision' }))[started ? 'toBeDisabled' : 'toBeEnabled']());
    if (started) {
      expect(screen.getByText(/Cannot retarget a started story/)).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Accept exact revision' }));
      expect(service.acceptAmendment).not.toHaveBeenCalled();
    }
  });

  it('shows started-node retarget refusal and cannot accept even if target fields are populated', async () => {
    const blocked = {
      ...impact, acceptable: false,
      blocking: [{ rule: 'amendment_definition_change', message: 'Cannot retarget a started story.', where: 'delivery/epic-1/wave-1/story-1' }],
      nodes: [{ address: 'delivery/epic-1/wave-1/story-1', change: 'modified', kind: 'story', title: 'Started API', state: 'running', attempts: 1,
        before: { repository_identity: 'delivery/api', provider_repository_id: 11 },
        after: { repository_identity: 'delivery/web', provider_repository_id: 12 },
      }],
    } as AmendmentImpact;
    vi.mocked(service.listAmendments).mockResolvedValue([reviewedDraft]);
    vi.mocked(service.getAmendment).mockResolvedValue(reviewedDraft);
    vi.mocked(service.previewAmendment).mockResolvedValue(blocked);
    renderPanel();
    fireEvent.change(await screen.findByLabelText('Pending amendment'), { target: { value: 'draft-1' } });
    const scope = await screen.findByRole('region', { name: 'Repository scope change for Started API' });
    expect(scope).toHaveTextContent('Accepted target: delivery/api');
    expect(scope).toHaveTextContent('Proposed target: delivery/web');
    expect(screen.getByText(/Cannot retarget a started story/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Accept exact revision' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Accept exact revision' }));
    expect(service.acceptAmendment).not.toHaveBeenCalled();
  });

  it('names the issues acceptance will create and the repository for each', async () => {
    const publishing = {
      ...impact,
      issue_publications: [
        { address: 'delivery/epic-1/wave-1/new', proposal_identity: 'p:new', repository: 'aws-e/adp', action: 'create', creates_issue: true },
        { address: 'delivery/epic-1/wave-1/old', proposal_identity: 'p:old', repository: 'aws-e/adp', action: 'reuse', creates_issue: false, issue_number: 41 },
      ],
    } as AmendmentImpact;
    const draft = { draft_id: 'draft-1', flow_id: 'flow-1', state: 'pending', proposal_hash: 'b'.repeat(64), base_plan_version: 2, base_plan_hash: 'a'.repeat(64), base_is_current: true, proposal_document: { flow_slug: 'delivery' } };
    vi.mocked(service.listAmendments).mockResolvedValue([draft]);
    vi.mocked(service.getAmendment).mockResolvedValue(draft);
    vi.mocked(service.previewAmendment).mockResolvedValue(publishing);
    renderPanel();
    fireEvent.change(await screen.findByLabelText('Pending amendment'), { target: { value: 'draft-1' } });

    const section = await screen.findByTestId('amendment-issue-publications');
    expect(section).toHaveTextContent('delivery/epic-1/wave-1/new → aws-e/adp: creates a new issue');
    expect(section).toHaveTextContent('delivery/epic-1/wave-1/old → aws-e/adp: uses issue #41');
  });
});


it('keeps the repository owner/name input reachable by keyboard without submitting an amendment', async () => {
  const user = userEvent.setup();
  renderPanel();
  const issue = screen.getByLabelText('Existing issue');
  issue.focus();
  await user.tab();
  expect(screen.getByLabelText('Story repository (owner/name)')).toHaveFocus();
  await user.keyboard('delivery/api');
  expect(screen.getByLabelText('Story repository (owner/name)')).toHaveValue('delivery/api');
  expect(service.acceptAmendment).not.toHaveBeenCalled();
  expect(service.proposeAmendment).not.toHaveBeenCalled();
});
