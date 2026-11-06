import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TransformRunsResults from '@/pages/products/services/TransformRunsResults';
import parity from '@/__tests__/fixtures/transform-client-parity.json';
import { checkTransformConfiguration, getTransformArtifact, getTransformResult, getTransformRun, listTransformBindings, listTransformResults, listTransformRuns, mutateTransformRun, readTransformPatch, submitTransformRun } from '@/services/l5Services';

vi.mock('@/services/l5Services', async importOriginal => ({
  ...await importOriginal<typeof import('@/services/l5Services')>(),
  checkTransformConfiguration: vi.fn(), getTransformArtifact: vi.fn(), getTransformResult: vi.fn(), getTransformRun: vi.fn(), listTransformBindings: vi.fn(),
  listTransformResults: vi.fn(), listTransformRuns: vi.fn(), mutateTransformRun: vi.fn(), readTransformPatch: vi.fn(), submitTransformRun: vi.fn(),
}));
const scope = { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '1' };
const source = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'source-one', record_type: 'repository_commit', record_id: 'example/widget', record_revision: 'abcdef123456' };
const configuration = { value: { configuration_id: 'config-one', revision: 3, scope, state: 'ready', settings: { enabled: true, source_refs: [source], capabilities: ['refactor'], transform: { goal_defaults: { kind: 'refactor', summary: 'Improve structure' } } } }, etag: '"config-three"' } as never;
const run = { run_id: 'run-one', scope, state: 'partial', provider_state: 'completed', revision: 4, monitoring: { state: 'degraded', progress_state: 'suspected_stalled', last_successful_poll_at: '2026-10-27T12:03:00Z', latest_observation: { progress_coverage: 'partial' } } };
const patch = { artifact_id: 'artifact-one', version: '1', sha256: 'a'.repeat(64), bytes: 7, media_type: 'text/x-diff', tenant_id: 'tenant-one', project_id: 'project-one', expires_at: '2026-11-30T00:00:00Z' };
const result = { service_id: 'transform', result_id: 'result-one', run_id: 'run-one', scope, revision: 2, verification_status: 'partial', scope_status: 'passed', dependency_comparison: { status: 'inconclusive' }, observed_at: '2026-10-27T12:00:00Z', retention_expires_at: patch.expires_at, cost_status: 'unknown', original_patch: { sha256: patch.sha256, bytes: patch.bytes }, retained_artifacts: { original_patch: patch }, tests_before: [{ name: 'baseline', status: 'passed' }], tests_after: [{ name: 'candidate', status: 'failed' }], limitations: ['test failed'] };
const binding = { binding_id: 'binding-one', configuration_id: 'config-one', state: 'ready', scope };
const props = { productId: 'product-one', projectId: 'project-one', tenantId: 'tenant-one', configuration, onAccessDenied: vi.fn() };

describe('Transform run and result evidence', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(checkTransformConfiguration).mockResolvedValue({ ready: true, gaps: [] });
    vi.mocked(listTransformBindings).mockResolvedValue({ items: [binding] } as never);
    vi.mocked(listTransformRuns).mockResolvedValue({ items: [run] } as never);
    vi.mocked(listTransformResults).mockResolvedValue({ items: [result] } as never);
    vi.mocked(getTransformRun).mockResolvedValue({ value: run, etag: '"run-four"' } as never);
    vi.mocked(getTransformResult).mockResolvedValue({ value: result, etag: '"result-two"' } as never);
    vi.mocked(getTransformArtifact).mockResolvedValue({ artifact: patch, download_path: '/ignored', expires_at: patch.expires_at });
    vi.mocked(readTransformPatch).mockResolvedValue('--- original\n+++ candidate\n');
  });
  it('uses current qualification instead of saved readiness and rechecks after configuration edits', async () => {
    vi.mocked(listTransformBindings).mockImplementationOnce(() => new Promise(resolve => {
      setTimeout(() => resolve({ items: [binding] } as never), 50);
    }));
    const saved = { ...configuration, value: { ...configuration.value, state: 'needs_input', gaps: ['executor_unqualified'] } };
    const { rerender } = render(<TransformRunsResults {...props} configuration={saved} />);
    await screen.findByRole('option', { name: 'binding-one' });
    await userEvent.selectOptions(screen.getByLabelText('Ready Transform binding'), 'binding-one');
    expect(await screen.findByText(/Current configuration readiness: ready/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start Transform run' })).toBeEnabled();
    vi.mocked(checkTransformConfiguration).mockResolvedValue({ ready: false, gaps: ['executor_qualification_stale'] });
    rerender(<TransformRunsResults {...props} configuration={{ ...saved, etag: '"edited"' }} />);
    expect(await screen.findByText(/Current configuration readiness: not ready; gaps: executor_qualification_stale/)).toBeInTheDocument();
    expect(checkTransformConfiguration).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'Start Transform run' })).toBeDisabled();
  });
  it('loads runs and results independently and inspects evidence beyond their first pages', async () => {
    vi.mocked(listTransformRuns).mockResolvedValueOnce({ items: [run], next_cursor: 'runs-next' } as never)
      .mockResolvedValueOnce({ items: [{ ...run, run_id: 'run-older' }] } as never);
    vi.mocked(listTransformResults).mockResolvedValueOnce({ items: [result], next_cursor: 'results-next' } as never)
      .mockResolvedValueOnce({ items: [{ ...result, result_id: 'result-older' }] } as never);
    vi.mocked(getTransformRun).mockResolvedValue({ value: { ...run, run_id: 'run-older' }, etag: '"run-older"' } as never);
    vi.mocked(getTransformResult).mockResolvedValue({ value: { ...result, result_id: 'result-older' }, etag: '"result-older"' } as never);
    render(<TransformRunsResults {...props} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Load more Transform runs' }));
    expect(listTransformRuns).toHaveBeenLastCalledWith('product-one', 'project-one', 'runs-next');
    expect(listTransformResults).toHaveBeenCalledTimes(1);
    await userEvent.click(await screen.findByRole('button', { name: /Run run-older:/ }));
    expect(getTransformRun).toHaveBeenLastCalledWith('product-one', 'project-one', 'run-older');
    await userEvent.click(screen.getByRole('button', { name: 'Load more Transform results' }));
    expect(listTransformResults).toHaveBeenLastCalledWith('product-one', 'project-one', 'results-next');
    await userEvent.click(await screen.findByRole('button', { name: /Result result-older:/ }));
    expect(getTransformResult).toHaveBeenLastCalledWith('product-one', 'project-one', 'result-older');
    await userEvent.click(screen.getByRole('button', { name: 'Inspect original diff' }));
    expect(await screen.findByLabelText('Original provider diff')).toHaveTextContent('--- original');
    expect(screen.getByRole('button', { name: /Run run-one:/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Result result-one:/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load more/ })).not.toBeInTheDocument();
  });
  it.each(['runs', 'results'] as const)('clears retained evidence and both cursors when access is denied paging %s', async list => {
    vi.mocked(listTransformRuns).mockResolvedValue({ items: [run], next_cursor: 'runs-next' } as never);
    vi.mocked(listTransformResults).mockResolvedValue({ items: [result], next_cursor: 'results-next' } as never);
    const onAccessDenied = vi.fn();
    render(<TransformRunsResults {...props} onAccessDenied={onAccessDenied} />);
    await userEvent.click(await screen.findByRole('button', { name: /Result result-one:/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Inspect original diff' }));
    await screen.findByLabelText('Original provider diff');
    if (list === 'runs') vi.mocked(listTransformRuns).mockRejectedValueOnce({ status: 403 });
    else vi.mocked(listTransformResults).mockRejectedValueOnce({ status: 404 });
    await userEvent.click(screen.getByRole('button', { name: `Load more Transform ${list}` }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledTimes(1));
    expect(screen.queryByLabelText('Original provider diff')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Run run-one:/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Result result-one:/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load more/ })).not.toBeInTheDocument();
  });
  it('renders the pinned scoped API evidence without treating an unknown run or failed check as passed', async () => {
    const scopedProps = { ...props, productId: 'product-example', projectId: 'project-example', tenantId: 'tenant-example',
      configuration: { ...configuration, value: { ...configuration.value, scope: parity.run.scope } } as never };
    vi.mocked(listTransformRuns).mockResolvedValue({ items: [parity.run] } as never);
    vi.mocked(listTransformResults).mockResolvedValue({ items: [parity.result] } as never);
    vi.mocked(listTransformBindings).mockResolvedValue({ items: [] } as never);
    vi.mocked(getTransformRun).mockResolvedValue({ value: parity.run, etag: '"2"' } as never);
    vi.mocked(getTransformResult).mockResolvedValue({ value: parity.result, etag: '"3"' } as never);
    vi.mocked(getTransformArtifact).mockResolvedValue({ artifact: parity.result.retained_artifacts.original_patch, download_path: '/ignored', expires_at: '2026-11-30T00:00:00Z' } as never);
    vi.mocked(readTransformPatch).mockResolvedValue(parity.patch_text);
    render(<TransformRunsResults {...scopedProps} />);
    await userEvent.click(await screen.findByRole('button', { name: /Run run-one: outcome_unknown/ }));
    expect(await screen.findByText(/Progress: unknown/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Result result-example: partial/ }));
    expect(await screen.findByText(/Check 1: failed/)).toBeInTheDocument();
    expect(screen.getByText('candidate test failed')).toBeInTheDocument();
    expect(screen.getByText(/cost unknown/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Inspect original diff' }));
    expect(await screen.findByLabelText('Original provider diff')).toHaveTextContent(/--- original\s+\+\+\+ candidate/);
    expect(readTransformPatch).toHaveBeenCalledWith('product-example', 'project-example', 'artifact-example', parity.result.original_patch.sha256);
    const unavailable = screen.getByRole('button', { name: 'Publish Transform PR (unavailable)' });
    expect(unavailable).toBeDisabled();
    await userEvent.click(unavailable);
    await userEvent.click(unavailable);
    expect(readTransformPatch).toHaveBeenCalledTimes(1);
    expect(submitTransformRun).not.toHaveBeenCalled();
    expect(mutateTransformRun).not.toHaveBeenCalled();
  });
  it('shows observed progress, independent failed tests, limitations and the verified original diff', async () => {
    render(<TransformRunsResults {...props} />);
    const runs = screen.getByRole('region', { name: 'Runs & schedules' });
    const results = screen.getByRole('region', { name: 'Results' });
    await userEvent.click(await within(runs).findByRole('button', { name: /Run run-one: partial/ }));
    expect(await screen.findByText(/Progress: suspected_stalled/)).toBeInTheDocument();
    await userEvent.click(within(results).getByRole('button', { name: /Result result-one: partial/ }));
    expect(await screen.findByText(/candidate: failed/)).toBeInTheDocument();
    expect(screen.getByText('test failed')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Inspect original diff' }));
    expect(await within(results).findByLabelText('Original provider diff')).toHaveTextContent('--- original');
    expect(within(runs).queryByLabelText('Original provider diff')).not.toBeInTheDocument();
    expect(readTransformPatch).toHaveBeenCalledWith('product-one', 'project-one', 'artifact-one', patch.sha256);
    expect(submitTransformRun).not.toHaveBeenCalled();
    expect(screen.getByText(/Repository PR publication is unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Publish Transform PR (unavailable)' })).toBeDisabled();
  });
  it('reports unqualified run admission as unavailable while retaining readable results', async () => {
    vi.mocked(listTransformRuns).mockRejectedValue({ status: 503 });
    render(<TransformRunsResults {...props} />);
    expect(await screen.findByText(/Runs: Unavailable \(HTTP 503\)/)).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /Result result-one/ })).toBeInTheDocument();
    expect(submitTransformRun).not.toHaveBeenCalled();
  });
  it('pins the configuration revision, source and ready binding for admission, then distinguishes accepted from passed', async () => {
    vi.mocked(submitTransformRun).mockResolvedValue({ value: { ...run, state: 'admitted', provider_state: 'not_started' }, etag: '"run-one"' } as never);
    vi.mocked(mutateTransformRun).mockResolvedValue({ value: run, etag: '"run-two"' } as never);
    render(<TransformRunsResults {...props} />);
    await screen.findByRole('option', { name: 'binding-one' });
    await userEvent.selectOptions(screen.getByLabelText('Ready Transform binding'), 'binding-one');
    await userEvent.click(screen.getByRole('button', { name: 'Start Transform run' }));
    await waitFor(() => expect(submitTransformRun).toHaveBeenCalledWith('product-one', 'project-one', {
      service_id: 'transform', configuration_revision: 3, binding_id: 'binding-one', capability: 'refactor',
      sources: [source], transform_input: { goal: { kind: 'refactor', summary: 'Improve structure' } },
    }));
    expect(await screen.findByText(/Run accepted. Admission is not a passed test/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Reason for lifecycle action'), 'Cancel requested');
    await userEvent.click(screen.getByRole('button', { name: 'Cancel run' }));
    await waitFor(() => expect(mutateTransformRun).toHaveBeenCalledWith('product-one', 'project-one', 'run-one', 'cancel', 'Cancel requested'));
  });
  it('clears all result data when a detail read loses scope and does not load patch content', async () => {
    vi.mocked(getTransformResult).mockRejectedValue({ status: 404 });
    const onAccessDenied = vi.fn();
    render(<TransformRunsResults {...props} onAccessDenied={onAccessDenied} />);
    await userEvent.click(await screen.findByRole('button', { name: /Result result-one/ }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: /Run run-one:/ })).not.toBeInTheDocument();
    expect(readTransformPatch).not.toHaveBeenCalled();
  });
  it('removes pinned result evidence when artifact authority is revoked before the diff read', async () => {
    const onAccessDenied = vi.fn();
    const scopedProps = { ...props, productId: 'product-example', projectId: 'project-example', tenantId: 'tenant-example',
      configuration: { ...configuration, value: { ...configuration.value, scope: parity.run.scope } } as never, onAccessDenied };
    vi.mocked(listTransformRuns).mockResolvedValue({ items: [parity.run] } as never);
    vi.mocked(listTransformResults).mockResolvedValue({ items: [parity.result] } as never);
    vi.mocked(listTransformBindings).mockResolvedValue({ items: [] } as never);
    vi.mocked(getTransformResult).mockResolvedValue({ value: parity.result, etag: '"3"' } as never);
    vi.mocked(getTransformArtifact).mockRejectedValue({ status: 404 });
    render(<TransformRunsResults {...scopedProps} />);
    await userEvent.click(await screen.findByRole('button', { name: /Result result-example: partial/ }));
    expect(await screen.findByText(/Check 1: failed/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Inspect original diff' }));
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('candidate test failed')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Result result-example/ })).not.toBeInTheDocument();
    expect(readTransformPatch).not.toHaveBeenCalled();
    expect(submitTransformRun).not.toHaveBeenCalled();
  });
  it('does not accept or replay a conflicting run submission', async () => {
    vi.mocked(submitTransformRun).mockRejectedValue({ status: 409 });
    render(<TransformRunsResults {...props} />);
    await screen.findByRole('option', { name: 'binding-one' });
    await userEvent.selectOptions(screen.getByLabelText('Ready Transform binding'), 'binding-one');
    await userEvent.click(screen.getByRole('button', { name: 'Start Transform run' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('HTTP 409');
    expect(screen.queryByText(/Run accepted/)).not.toBeInTheDocument();
    expect(submitTransformRun).toHaveBeenCalledTimes(1);
    expect(mutateTransformRun).not.toHaveBeenCalled();
    expect(readTransformPatch).not.toHaveBeenCalled();
  });
});
