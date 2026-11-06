import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SecurityResults from '@/pages/products/services/SecurityResults';
import { compareSecurityResults, getSecurityPublication, getSecurityResult, listSecurityFindings, listSecurityResults, listSecurityTriage, retrySecurityPublication, setSecurityTriage, type SecurityResult } from '@/services/l5Services';

vi.mock('@/services/l5Services', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/l5Services')>(), compareSecurityResults: vi.fn(), getSecurityPublication: vi.fn(), getSecurityResult: vi.fn(), listSecurityFindings: vi.fn(), listSecurityResults: vi.fn(), listSecurityTriage: vi.fn(), retrySecurityPublication: vi.fn(), setSecurityTriage: vi.fn() }));
const source = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'document', record_id: 'strategy', record_revision: '17' };
const partial: SecurityResult = { scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' }, result_id: 'result-one', run_id: 'run-one', revision: 2, native_job_ref: source, observed_at: '2026-10-27T12:00:00Z', coverage: 'partial', coverage_mode: 'documents', finding_count: 1, provider_outcome: 'failed', publication_id: 'publication-one', admission: { adapter_revision: 'v1', binding_generation: 2, source_snapshots: [{ source_ref: source, snapshot_id: 'snapshot-one' }] } };
const finding = { finding_id: 'finding-one', native_id: 'native-one', kind: 'candidate_threat' as const, claim: 'Review access to source', provider_severity: 'high', validation: 'candidate' as const,
  source_refs: [source], evidence: [{ artifact_id: 'evidence-one', version: '3' }], locations: ['architecture'], assumptions: ['The source is current'], mitigations: ['Review access'] };
const configuration = { value: { scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' }, configuration_id: 'config-one' }, etag: '"config-one"' } as never;
const publication = { publication_id: 'publication-one', result_id: 'result-one', state: 'retry_wait' as const, contribution_hash: 'hash-one', invalidation_watermark: 0 };
const mount = (onAccessDenied?: () => void) => render(<SecurityResults productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
const selectResult = async () => {
  const select = await screen.findByLabelText('Visible results');
  await within(select).findByRole('option', { name: /result-one$/ });
  await userEvent.selectOptions(select, 'result-one');
};

describe('Security results', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listSecurityResults).mockResolvedValue({ items: [partial, { ...partial, result_id: 'baseline-one', coverage: 'complete' }] });
    vi.mocked(getSecurityResult).mockResolvedValue(partial);
    vi.mocked(listSecurityFindings).mockResolvedValue({ items: [finding] });
    vi.mocked(getSecurityPublication).mockResolvedValue(publication);
    vi.mocked(listSecurityTriage).mockResolvedValue({ value: { items: [] }, etag: '"empty-triage-head"' });
    vi.mocked(setSecurityTriage).mockResolvedValue({ value: { finding_id: 'finding-one', revision: 1, actor_id: 'actor-one', observed_at: '2026-10-27T12:01:00Z', decision: { decision: 'open', reason: 'Needs evidence' } }, etag: '"triage-one"' });
  });

  it('keeps incomplete coverage, candidate evidence and publication failure separate from triage', async () => {
    vi.mocked(compareSecurityResults).mockResolvedValue({ comparability: 'partial', reasons: ['Unscanned sources'], baseline_revision: 1, candidate_revision: 2, resolved_findings: ['finding-two'] });
    mount(); await selectResult();
    const detail = await screen.findByLabelText('Security result detail');
    expect(within(detail).getByText(/coverage partial \(documents\); provider outcome failed/)).toBeInTheDocument();
    expect(within(detail).getByText(/Reported finding count: 1.*Coverage is incomplete/)).toBeInTheDocument();
    expect(within(detail).getByText(/Source snapshots: document strategy \(revision 17\)/)).toBeInTheDocument();
    expect(within(detail).getByText(/Publication state: retry_wait.*not confirmed/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Baseline result'), 'baseline-one');
    await userEvent.click(screen.getByRole('button', { name: 'Compare results' }));
    expect(await screen.findByText(/Resolved findings cannot be inferred/)).toBeInTheDocument();
    expect(screen.queryByText(/Reported resolved finding IDs: finding-two/)).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Visible findings'), 'finding-one');
    expect(await screen.findByText(/Candidate threat finding-one/)).toBeInTheDocument();
    expect(screen.getByText(/validation: candidate; human triage: not recorded/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Triage reason'), 'Needs evidence');
    await userEvent.click(screen.getByRole('button', { name: 'Save triage decision' }));
    await waitFor(() => expect(setSecurityTriage).toHaveBeenCalledWith('product-one', 'project-one', 'result-one', 'finding-one', { decision: 'open', reason: 'Needs evidence' }, '"empty-triage-head"'));
    expect(screen.getByText(/human triage: open/)).toBeInTheDocument();
  });

  it('does not call zero findings a passing scan even with completed coverage', async () => {
    vi.mocked(getSecurityResult).mockResolvedValue({ ...partial, finding_count: 0, coverage: 'complete', provider_outcome: 'completed' });
    vi.mocked(listSecurityFindings).mockResolvedValue({ items: [] });
    mount(); await selectResult();
    expect(await screen.findByText(/Reported finding count: 0.*not proof that all threats were found/)).toBeInTheDocument();
    expect(screen.getByText(/Zero findings does not prove a passed scan/)).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /finding-one/ })).not.toBeInTheDocument();
  });

  it('shows a truthful empty state and clears counts when source access is denied', async () => {
    vi.mocked(listSecurityResults).mockResolvedValueOnce({ items: [] }).mockResolvedValueOnce({ items: [partial] });
    mount();
    expect(await screen.findByText(/No Security results are visible.*does not establish a completed/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Security results' }));
    vi.mocked(listSecurityFindings).mockRejectedValueOnce({ status: 403 });
    await selectResult();
    expect(await screen.findByRole('alert')).toHaveTextContent('Result, findings or publication are unavailable');
    expect(screen.queryByText(/Reported finding count: 1/)).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'result-one' })).not.toBeInTheDocument();
    expect(setSecurityTriage).not.toHaveBeenCalled();
  });

  it('reuses the original publication receipt after an uncertain retry without starting another assessment', async () => {
    vi.mocked(retrySecurityPublication).mockRejectedValueOnce(new Error('response lost')).mockResolvedValueOnce({ ...publication, state: 'published', receipt_id: 'receipt-one', consumer_revision: '9' });
    mount(); await selectResult();
    await screen.findByText(/Publication state: retry_wait/);
    await userEvent.type(screen.getByLabelText('Publication retry reason'), 'Recover delivery');
    await userEvent.click(screen.getByRole('button', { name: 'Retry publication only' }));
    const first = vi.mocked(retrySecurityPublication).mock.calls[0];
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original publication receipt' }));
    await waitFor(() => expect(retrySecurityPublication).toHaveBeenCalledTimes(2));
    expect(vi.mocked(retrySecurityPublication).mock.calls[1]).toEqual(first);
    expect(await screen.findByText(/Publication state: published.*Receipt receipt-one; consumer revision 9/)).toBeInTheDocument();
  });

  it('requires a fresh triage head after a 412 rather than silently overwriting the decision', async () => {
    vi.mocked(setSecurityTriage).mockRejectedValueOnce({ status: 412 });
    mount(); await selectResult();
    await userEvent.selectOptions(await screen.findByLabelText('Visible findings'), 'finding-one');
    await userEvent.type(await screen.findByLabelText('Triage reason'), 'Needs evidence');
    await userEvent.click(screen.getByRole('button', { name: 'Save triage decision' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Refresh its revision');
    expect(screen.queryByRole('button', { name: 'Save triage decision' })).not.toBeInTheDocument();
    expect(setSecurityTriage).toHaveBeenCalledTimes(1);
    vi.mocked(listSecurityTriage).mockResolvedValueOnce({ value: { items: [{ finding_id: 'finding-one', revision: 2, actor_id: 'cli-actor',
      observed_at: '2026-10-27T12:02:00Z', decision: { decision: 'false_positive', reason: 'Checked from CLI' } }] }, etag: '"triage-two"' });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh finding triage' }));
    expect(await screen.findByText(/human triage: false_positive/)).toBeInTheDocument();
    expect(screen.getByLabelText('Triage decision')).toHaveValue('false_positive');
    expect(screen.getByRole('button', { name: 'Save triage decision' })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText('Triage decision'), 'accepted_risk');
    await userEvent.type(screen.getByLabelText('Triage reason'), 'Re-evaluated after CLI update');
    await userEvent.click(screen.getByRole('button', { name: 'Save triage decision' }));
    await waitFor(() => expect(setSecurityTriage).toHaveBeenCalledTimes(2));
    expect(vi.mocked(setSecurityTriage).mock.calls[1]).toEqual(['product-one', 'project-one', 'result-one', 'finding-one',
      { decision: 'accepted_risk', reason: 'Re-evaluated after CLI update' }, '"triage-two"']);
  });

  it.each(['finding', 'result'])('discards triage from the previous %s even with equal ETags', async (selectionKind) => {
    const secondFindingId = selectionKind === 'finding' ? 'finding-two' : 'finding-one';
    const secondResultId = selectionKind === 'result' ? 'baseline-one' : 'result-one';
    const oldDecision = { finding_id: 'finding-one', revision: 1, actor_id: 'actor-one', observed_at: '2026-10-27T12:01:00Z',
      decision: { decision: 'accepted_risk' as const, reason: 'Old finding decision' } };
    const newDecision = { ...oldDecision, finding_id: secondFindingId, decision: { decision: 'open' as const, reason: 'Current decision' } };
    let finishPrevious!: (value: Awaited<ReturnType<typeof listSecurityTriage>>) => void;
    const previous = new Promise<Awaited<ReturnType<typeof listSecurityTriage>>>(resolve => { finishPrevious = resolve; });
    vi.mocked(listSecurityFindings).mockResolvedValue({ items: [finding, { ...finding, finding_id: 'finding-two' }] });
    vi.mocked(getSecurityResult).mockImplementation(async (_product, _project, resultId) => ({ ...partial, result_id: resultId, publication_id: undefined }));
    vi.mocked(listSecurityTriage).mockResolvedValue({ value: { items: [newDecision] }, etag: '"1"' }).mockReturnValueOnce(previous);
    vi.mocked(setSecurityTriage).mockResolvedValue({ value: { ...newDecision, revision: 2 }, etag: '"2"' });
    mount(); await selectResult();
    await userEvent.selectOptions(await screen.findByLabelText('Visible findings'), 'finding-one');
    expect(screen.queryByRole('button', { name: 'Save triage decision' })).not.toBeInTheDocument();
    if (selectionKind === 'result') await userEvent.selectOptions(screen.getByLabelText('Visible results'), secondResultId);
    await userEvent.selectOptions(await screen.findByLabelText('Visible findings'), secondFindingId);
    expect(await screen.findByLabelText('Triage decision')).toHaveValue('open');
    await act(async () => { finishPrevious({ value: { items: [oldDecision] }, etag: '"1"' }); });
    expect(screen.getByLabelText('Visible results')).toHaveValue(secondResultId);
    expect(screen.getByLabelText('Visible findings')).toHaveValue(secondFindingId);
    expect(screen.getByLabelText('Triage decision')).toHaveValue('open');
    await userEvent.type(screen.getByLabelText('Triage reason'), 'Review current finding');
    await userEvent.click(screen.getByRole('button', { name: 'Save triage decision' }));
    expect(setSecurityTriage).toHaveBeenCalledExactlyOnceWith('product-one', 'project-one', secondResultId, secondFindingId,
      { decision: 'open', reason: 'Review current finding' }, '"1"');
  });

  it('ignores late result detail before loading triage for the current result', async () => {
    let finishPrevious!: (value: SecurityResult) => void;
    const previous = new Promise<SecurityResult>(resolve => { finishPrevious = resolve; });
    vi.mocked(getSecurityResult).mockResolvedValue({ ...partial, result_id: 'baseline-one', publication_id: undefined }).mockReturnValueOnce(previous);
    mount(); await selectResult();
    await userEvent.selectOptions(screen.getByLabelText('Visible results'), 'baseline-one');
    await screen.findByLabelText('Visible findings');
    await act(async () => { finishPrevious(partial); });
    expect(listSecurityFindings).toHaveBeenCalledExactlyOnceWith('product-one', 'project-one', 'baseline-one');
    await userEvent.selectOptions(screen.getByLabelText('Visible findings'), 'finding-one');
    await screen.findByLabelText('Triage decision');
    expect(listSecurityTriage).toHaveBeenCalledExactlyOnceWith('product-one', 'project-one', 'baseline-one', 'finding-one');
  });

  it.each(['success', 'conflict'])('ignores a previous finding write %s after selection changes', async (outcome) => {
    let finishPrevious!: (value: Awaited<ReturnType<typeof setSecurityTriage>>) => void;
    let failPrevious!: (reason: unknown) => void;
    const previous = new Promise<Awaited<ReturnType<typeof setSecurityTriage>>>((resolve, reject) => { finishPrevious = resolve; failPrevious = reject; });
    vi.mocked(listSecurityFindings).mockResolvedValue({ items: [finding, { ...finding, finding_id: 'finding-two' }] });
    vi.mocked(listSecurityTriage).mockResolvedValue({ value: { items: [] }, etag: '"1"' });
    vi.mocked(setSecurityTriage).mockResolvedValue({ value: { finding_id: 'finding-two', revision: 1, actor_id: 'actor-one', observed_at: '2026-10-27T12:01:00Z',
      decision: { decision: 'open', reason: 'Current finding reason' } }, etag: '"2"' }).mockReturnValueOnce(previous);
    mount(); await selectResult();
    await userEvent.selectOptions(await screen.findByLabelText('Visible findings'), 'finding-one');
    await userEvent.selectOptions(await screen.findByLabelText('Triage decision'), 'accepted_risk');
    await userEvent.type(screen.getByLabelText('Triage reason'), 'Decision for previous finding');
    await userEvent.click(screen.getByRole('button', { name: 'Save triage decision' }));
    await userEvent.selectOptions(screen.getByLabelText('Visible findings'), 'finding-two');
    await userEvent.type(await screen.findByLabelText('Triage reason'), 'Current finding reason');
    await act(async () => {
      if (outcome === 'conflict') failPrevious({ status: 412 });
      else finishPrevious({ value: { finding_id: 'finding-one', revision: 2, actor_id: 'actor-one', observed_at: '2026-10-27T12:01:00Z',
        decision: { decision: 'accepted_risk', reason: 'Decision for previous finding' } }, etag: '"2"' });
    });
    expect(screen.getByLabelText('Triage reason')).toHaveValue('Current finding reason');
    expect(screen.getByLabelText('Triage decision')).toHaveValue('open');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save triage decision' }));
    expect(vi.mocked(setSecurityTriage).mock.calls[1]).toEqual(['product-one', 'project-one', 'result-one', 'finding-two',
      { decision: 'open', reason: 'Current finding reason' }, '"1"']);
  });
  it('hides all result choices when a list includes a foreign Product or lacks a scope', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(listSecurityResults).mockResolvedValue({ items: [partial, { ...partial, result_id: 'foreign-result', scope: { ...partial.scope, product_id: 'product-other' }, finding_count: 93 }] });
    mount(onAccessDenied);
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalled());
    expect(screen.queryByRole('option', { name: /result-one|foreign-result/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Reported finding count:/)).not.toBeInTheDocument();
    expect(getSecurityResult).not.toHaveBeenCalled();
  });

  it('erases visible results when access is revoked on refresh', async () => {
    const onAccessDenied = vi.fn();
    mount(onAccessDenied); await selectResult();
    expect(await screen.findByText(/Reported finding count: 1/)).toBeInTheDocument();
    vi.mocked(listSecurityResults).mockRejectedValueOnce({ status: 403 });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Security results' }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalled());
    expect(screen.queryByText(/Reported finding count:/)).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /result-one/ })).not.toBeInTheDocument();
  });

  it('rejects findings whose source belongs to another tenant before rendering counts', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(listSecurityFindings).mockResolvedValue({ items: [{ ...finding, source_refs: [{ ...source, tenant_id: 'tenant-other' }] }] });
    mount(onAccessDenied); await selectResult();
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalled());
    expect(screen.queryByText(/Reported finding count:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Review access to source/)).not.toBeInTheDocument();
  });

  it.each([{ status: 503 }, new Error('connection lost')])('retains the result and findings when publication readback fails: %s', async (error) => {
    vi.mocked(getSecurityPublication).mockRejectedValueOnce(error);
    mount(); await selectResult();
    expect(await screen.findByRole('alert')).toHaveTextContent('The retained result and findings remain available');
    expect(screen.getByText(/Reported finding count: 1/)).toBeInTheDocument();
    expect(screen.getByText(/Publication state: unavailable/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Visible findings'), 'finding-one');
    expect(await screen.findByText(/Candidate threat finding-one/)).toBeInTheDocument();
  });

  it('clears retained result details when publication readback denies access', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(getSecurityPublication).mockRejectedValueOnce({ status: 403 });
    mount(onAccessDenied); await selectResult();
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalled());
    expect(screen.queryByLabelText('Security result detail')).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /result-one/ })).not.toBeInTheDocument();
  });

  it('loads retained result and finding pages without duplicate choices', async () => {
    vi.mocked(listSecurityResults).mockResolvedValueOnce({ items: [partial], next_cursor: 'results-next' })
      .mockResolvedValueOnce({ items: [partial, { ...partial, result_id: 'result-two' }] });
    vi.mocked(listSecurityFindings).mockResolvedValueOnce({ items: [finding], next_cursor: 'findings-next' })
      .mockResolvedValueOnce({ items: [finding, { ...finding, finding_id: 'finding-two' }] });
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Load more results' }));
    expect(listSecurityResults).toHaveBeenLastCalledWith('product-one', 'project-one', 'results-next');
    const choices = screen.getByLabelText('Visible results');
    expect(within(choices).getAllByRole('option')).toHaveLength(3);
    expect(screen.queryByRole('button', { name: 'Load more results' })).not.toBeInTheDocument();
    await selectResult();
    await userEvent.click(await screen.findByRole('button', { name: 'Load more findings' }));
    expect(listSecurityFindings).toHaveBeenLastCalledWith('product-one', 'project-one', 'result-one', 'findings-next');
    expect(within(screen.getByLabelText('Visible findings')).getAllByRole('option')).toHaveLength(3);
    expect(screen.queryByRole('button', { name: 'Load more findings' })).not.toBeInTheDocument();
  });

  it('ignores a stale result page after refreshing the list', async () => {
    let finish!: (value: Awaited<ReturnType<typeof listSecurityResults>>) => void;
    const previous = new Promise<Awaited<ReturnType<typeof listSecurityResults>>>(resolve => { finish = resolve; });
    vi.mocked(listSecurityResults).mockResolvedValueOnce({ items: [partial], next_cursor: 'results-next' })
      .mockReturnValueOnce(previous).mockResolvedValueOnce({ items: [{ ...partial, result_id: 'current-result' }] });
    mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Load more results' }));
    expect(screen.getByRole('button', { name: 'Load more results' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Refresh Security results' }));
    await screen.findByRole('option', { name: /current-result/ });
    await act(async () => { finish({ items: [{ ...partial, result_id: 'old-result' }], next_cursor: 'obsolete' }); });
    expect(screen.queryByRole('option', { name: /old-result/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more results' })).not.toBeInTheDocument();
  });

  it.each(['success', 'denial'])('ignores a previous result findings page %s after selecting another result', async (outcome) => {
    let finish!: (value: Awaited<ReturnType<typeof listSecurityFindings>>) => void;
    let reject!: (reason: unknown) => void;
    const previous = new Promise<Awaited<ReturnType<typeof listSecurityFindings>>>((resolve, fail) => { finish = resolve; reject = fail; });
    vi.mocked(getSecurityResult).mockImplementation(async (_product, _project, resultId) => ({ ...partial, result_id: resultId, publication_id: undefined }));
    vi.mocked(listSecurityFindings).mockResolvedValueOnce({ items: [finding], next_cursor: 'findings-next' })
      .mockReturnValueOnce(previous).mockResolvedValueOnce({ items: [{ ...finding, finding_id: 'current-finding' }] });
    const onAccessDenied = vi.fn();
    mount(onAccessDenied); await selectResult();
    await userEvent.click(await screen.findByRole('button', { name: 'Load more findings' }));
    await userEvent.selectOptions(screen.getByLabelText('Visible results'), 'baseline-one');
    await screen.findByRole('option', { name: /current-finding/ });
    await act(async () => {
      if (outcome === 'denial') reject({ status: 403 });
      else finish({ items: [{ ...finding, finding_id: 'old-finding' }], next_cursor: 'obsolete' });
    });
    expect(screen.queryByRole('option', { name: /old-finding/ })).not.toBeInTheDocument();
    expect(screen.getByRole('option', { name: /current-finding/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more findings' })).not.toBeInTheDocument();
    expect(onAccessDenied).not.toHaveBeenCalled();
  });

  it.each(['result', 'baseline'])('ignores a comparison after changing the selected %s', async (selection) => {
    let finish!: (value: Awaited<ReturnType<typeof compareSecurityResults>>) => void;
    vi.mocked(compareSecurityResults).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    vi.mocked(getSecurityResult).mockImplementation(async (_product, _project, resultId) => ({ ...partial, result_id: resultId, publication_id: undefined }));
    mount(); await selectResult();
    await userEvent.selectOptions(await screen.findByLabelText('Baseline result'), 'baseline-one');
    await userEvent.click(screen.getByRole('button', { name: 'Compare results' }));
    await userEvent.selectOptions(screen.getByLabelText(selection === 'result' ? 'Visible results' : 'Baseline result'), selection === 'result' ? 'baseline-one' : '');
    await act(async () => { finish({ comparability: 'partial', reasons: ['Previous comparison only'] }); });
    expect(screen.queryByText(/Previous comparison only/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Result comparison')).not.toBeInTheDocument();
  });

  it('does not attach a late publication retry receipt to another result', async () => {
    let finish!: (value: Awaited<ReturnType<typeof retrySecurityPublication>>) => void;
    vi.mocked(retrySecurityPublication).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    vi.mocked(getSecurityResult).mockResolvedValueOnce(partial).mockResolvedValueOnce({ ...partial, result_id: 'baseline-one', publication_id: undefined });
    mount(); await selectResult();
    await userEvent.type(await screen.findByLabelText('Publication retry reason'), 'Recover delivery');
    await userEvent.click(screen.getByRole('button', { name: 'Retry publication only' }));
    await userEvent.selectOptions(screen.getByLabelText('Visible results'), 'baseline-one');
    await screen.findByText(/Publication state: not requested/);
    await act(async () => { finish({ ...publication, state: 'published', receipt_id: 'previous-receipt', consumer_revision: '9' }); });
    expect(screen.queryByText(/previous-receipt/)).not.toBeInTheDocument();
    expect(screen.getByText(/Publication state: not requested/)).toBeInTheDocument();
    expect(retrySecurityPublication).toHaveBeenCalledTimes(1);
  });

  it('renders the native threat-model response and opens findings by their claim', async () => {
    const nativeResult: SecurityResult = { ...partial, coverage: 'complete', provider_outcome: undefined, publication_id: undefined,
      native_job_ref: { tenant_id: 'tenant-one', project_id: 'project-one', agent_space_id: 'space-one',
        artifact_id: 'artifact-one', threat_model_id: 'model-one', threat_model_job_id: 'threat-job-one' } };
    vi.mocked(listSecurityResults).mockResolvedValue({ items: [nativeResult] });
    vi.mocked(getSecurityResult).mockResolvedValue(nativeResult);
    mount(); await selectResult();
    expect(await screen.findByRole('heading', { name: 'Threat modeling' })).toBeInTheDocument();
    expect(screen.getByLabelText('Result summary')).toHaveTextContent('Source coverage');
    expect(screen.getByLabelText('Result summary')).not.toHaveTextContent(/unknown|not requested|Result delivery/);
    expect(screen.getByText(/Result result-one, revision/)).not.toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: /Review access to source.*Candidate threat.*high/ }));
    const detail = await screen.findByLabelText('Finding detail');
    expect(within(detail).getByRole('heading', { name: 'Recommended mitigation' })).toBeVisible();
    expect(within(detail).getByText('Review access')).toBeVisible();
    expect(within(detail).getByText(/native native-one/)).not.toBeVisible();
    expect(listSecurityTriage).toHaveBeenCalledWith('product-one', 'project-one', 'result-one', 'finding-one');
    expect(retrySecurityPublication).not.toHaveBeenCalled();
  });

  it('labels full snapshot results as code scanning without calling incomplete coverage successful', async () => {
    vi.mocked(listSecurityResults).mockResolvedValue({ items: [{ ...partial, coverage_mode: 'full_snapshot' }] });
    vi.mocked(getSecurityResult).mockResolvedValue({ ...partial, coverage_mode: 'full_snapshot' });
    mount(); await selectResult();
    expect(await screen.findByRole('heading', { name: 'Code scanning' })).toBeVisible();
    expect(screen.getByText('Coverage is incomplete or unknown. Review the findings, but do not treat unchecked sources as clean.')).toBeVisible();
  });

  it('renders native code locations as file and line references instead of React objects', async () => {
    vi.mocked(getSecurityResult).mockResolvedValue({ ...partial, coverage_mode: 'full_snapshot' });
    vi.mocked(listSecurityFindings).mockResolvedValue({ items: [{ ...finding, kind: 'static_finding',
      locations: [{ filePath: 'src/auth.ts', lineStart: 21, lineEnd: 29 }, { filePath: 'config/policy.yaml' }] }] });
    mount(); await selectResult();
    await userEvent.click(await screen.findByRole('button', { name: /Review access to source.*src\/auth.ts:21–29/ }));
    const detail = await screen.findByLabelText('Finding detail');
    expect(within(detail).getByText(/Locations: src\/auth.ts:21–29/)).toBeInTheDocument();
    expect(within(detail).getByText(/config\/policy.yaml/)).toBeInTheDocument();
    expect(screen.queryByText(/undefined|\[object Object\]/)).not.toBeInTheDocument();
  });

});
