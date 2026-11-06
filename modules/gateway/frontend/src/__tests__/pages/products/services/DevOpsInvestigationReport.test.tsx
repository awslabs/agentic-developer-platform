import { createHash, webcrypto } from 'node:crypto';
import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DevOpsInvestigationReport from '@/pages/products/services/DevOpsInvestigationReport';
import type { DevOpsResult } from '@/services/l5Services';

vi.mock('@/services/auth', () => ({ getAccessToken: () => 'offline-token', clearTokens: vi.fn() }));
const scope = { tenant_id: 'tenant', product_id: 'product', project_id: 'project', relationship_revision: '1', owner_id: 'owner' };
const base = '/v1/products/product/projects/project/services/devops';
const fetchMock = vi.fn();
const onAccessDenied = vi.fn();

function fixture(origin: 'adp_submitted' | 'native' = 'adp_submitted', description = 'Investigate the timeout change', journal?: Record<string, unknown>[]) {
  const native = { tenant_id: 'tenant', project_id: 'project', source_connection_id: 'connection', record_type: 'devops_execution',
    record_id: 'execution', record_revision: origin === 'native' ? 'execution' : 'task' };
  const task = { agentSpaceId: 'space', executionId: 'execution', taskId: 'task', status: 'COMPLETED', description };
  const evidence = origin === 'native' ? task : { kind: 'devops_investigation', native_job_ref: native, task,
    execution: { agentSpaceId: 'space', executionId: 'execution', executionStatus: 'STOPPED' },
    journal_records: journal ?? [{ agentSpaceId: 'space', executionId: 'execution', recordId: 'record', content: { summary: 'Configuration changed' } }],
    operational_recommendations: [{ recommendationId: 'recommendation', taskId: 'task', version: 1, title: 'Restore configuration',
      content: { summary: 'Restore the previous timeout' }, priority: 'HIGH', status: 'OPEN' }],
    usage_coverage: { status: 'unavailable', reason: 'Native task API does not expose measured task cost' } };
  const bytes = JSON.stringify(evidence);
  const artifact = { artifact_id: 'readback-result', version: '1', media_type: 'application/json' as const,
    sha256: createHash('sha256').update(bytes).digest('hex'), bytes: Buffer.byteLength(bytes) };
  const result: DevOpsResult = { scope, result_id: 'result', run_id: 'run', revision: 1, service_id: 'devops', origin,
    native_job_ref: native, ...(origin === 'adp_submitted' ? { native_task_id: 'task' } : {}), evidence_ref: artifact,
    finding_count: 0, coverage: 'unknown', coverage_mode: 'native_observation', provider_outcome: 'completed', observed_at: '2026-11-01T00:00:00Z' };
  return { result, evidence, bytes, artifact };
}

function responses(value = fixture()) {
  return [
    new Response(JSON.stringify(value.result), { headers: { ETag: '"1"' } }),
    new Response(JSON.stringify({ artifact: value.artifact, download_path: `${base}/artifacts/readback-result/content`,
      expires_at: new Date(Date.now() + 60000).toISOString() })),
    new Response(value.bytes, { headers: { 'Content-Type': 'application/json', 'X-Adp-Content-Sha256': value.artifact.sha256 } }),
  ];
}
function queue(items: Response[]) { items.forEach(response => fetchMock.mockResolvedValueOnce(response)); }
function props(result = fixture().result) { return { productId: 'product', projectId: 'project', scope, result, onAccessDenied }; }
async function read() { await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Read investigation report' })); }); }

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('crypto', webcrypto);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('retained DevOps investigation report', () => {
  it('reads the actual zero-findings investigation through authenticated scoped endpoints and renders human-readable observations', async () => {
    const value = fixture(); queue(responses(value));
    render(<StrictMode><DevOpsInvestigationReport {...props(value.result)} /></StrictMode>);
    expect(fetchMock).not.toHaveBeenCalled();
    await read();
    expect(await screen.findByText('Restore the previous timeout')).toBeInTheDocument();
    expect(screen.getByText('Configuration changed')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Restore configuration' })).toBeInTheDocument();
    expect(screen.getByText('Investigate the timeout change')).toBeInTheDocument();
    expect(screen.getByText('Complete retained evidence').closest('details')).not.toHaveAttribute('open');
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api' + base + '/results/result', '/api' + base + '/artifacts/readback-result', '/api' + base + '/artifacts/readback-result/content',
    ]);
    expect(fetchMock.mock.calls.every(([, options]) => options.method === 'GET' && options.redirect === 'error'
      && options.headers.Authorization === 'Bearer offline-token')).toBe(true);
  });

  it('reads a report when CloudFront compression weakens its revision ETag', async () => {
    const value = fixture(); const replies = responses(value);
    replies[0] = new Response(JSON.stringify(value.result), { headers: { ETag: 'W/"1"' } });
    queue(replies); render(<DevOpsInvestigationReport {...props(value.result)} />); await read();
    expect(await screen.findByText('Restore the previous timeout')).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each(['W/"2"', 'W/"01"', '"1", "2"', '*'])('rejects a different or malformed revision ETag %s', async etag => {
    const value = fixture(); const replies = responses(value);
    replies[0] = new Response(JSON.stringify(value.result), { headers: { ETag: etag } });
    queue(replies); render(<DevOpsInvestigationReport {...props(value.result)} />); await read();
    expect(await screen.findByRole('alert')).toHaveTextContent('selected result changed');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('supports native-origin evidence without inventing journal observations or recommendations', async () => {
    const value = fixture('native'); queue(responses(value)); render(<DevOpsInvestigationReport {...props(value.result)} />);
    await read();
    expect(await screen.findByText('Investigate the timeout change')).toBeInTheDocument();
    expect(screen.getByText('No journal observations were retained in this report.')).toBeInTheDocument();
    expect(screen.getByText('No operational recommendations were retained in this report.')).toBeInTheDocument();
  });

  it('reads native JSON findings, uses the latest observation and leaves execution internals in collapsed evidence', async () => {
    const value = fixture('adp_submitted', 'Investigate the timeout change', [
      { recordType: 'observation', createdAt: '2026-11-01T00:00:00Z', content: JSON.stringify({
        type: 'observation', id: 'observation', title: 'Earlier finding', analysis: 'Earlier analysis' }) },
      { recordType: 'observation', createdAt: '2026-11-01T00:01:00Z', content: JSON.stringify({
        type: 'observation', id: 'observation', title: 'Recurring timeout', analysis: 'Requests still time out.', signals: [], plan_id: null }) },
      ...['message', 'utilization', 'ui_investigation_summary'].map(recordType => ({ recordType,
        content: JSON.stringify({ title: 'Internal diagnostic', summary: 'Provider execution detail' }) })),
      { recordType: 'investigation_summary_md', content: '## Confirmed findings\n\nTimeouts recur.' },
      { recordType: 'investigation_summary', content: JSON.stringify({ symptoms: ['Duplicate summary'] }) },
      { recordType: 'mitigation_summary_md', content: '## Review the configuration\n\nValidate the suspected cause before changing it.' },
    ]);
    queue(responses(value)); render(<DevOpsInvestigationReport {...props(value.result)} />); await read();
    const summary = await screen.findByText('Investigation observations (1)');
    fireEvent.click(summary);
    expect(screen.getByRole('heading', { name: 'Recurring timeout' })).toBeVisible();
    expect(screen.getByText('Requests still time out.')).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'Earlier finding' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Internal diagnostic' })).not.toBeInTheDocument();
    expect(screen.getByText(/Provider execution detail/).closest('details')).not.toHaveAttribute('open');
    expect(screen.getByText('Investigation summary').closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('heading', { name: 'Confirmed findings' })).toBeVisible();
    expect(screen.getByText('Suggested next steps').closest('details')).toHaveAttribute('open');
    expect(screen.getByText('Validate the suspected cause before changing it.')).toBeVisible();
    expect(screen.getByText(/Duplicate summary/).closest('details')).not.toHaveAttribute('open');
  });

  it('shows the question from the real ADP request envelope and keeps request metadata in collapsed evidence', async () => {
    const value = fixture('adp_submitted', JSON.stringify({ question: 'Why did requests time out?', run_id: 'run', idempotency_token: 'request-token' }));
    queue(responses(value)); render(<DevOpsInvestigationReport {...props(value.result)} />); await read();
    expect(await screen.findByText('Why did requests time out?')).toBeVisible();
    expect(screen.getByText(/request-token/).closest('details')).not.toHaveAttribute('open');
    expect(screen.getByText('Restore the previous timeout')).toBeVisible();
  });

  it('renders report strings as text without executing provider HTML or turning URLs into links', async () => {
    const value = fixture('native');
    value.evidence.description = '## Findings\n\n<img src=x onerror=alert(1)> https://untrusted.invalid\n\n[Unsafe link](https://untrusted.invalid) ![External image](https://untrusted.invalid/image.png)';
    value.bytes = JSON.stringify(value.evidence);
    value.artifact.sha256 = createHash('sha256').update(value.bytes).digest('hex');
    value.artifact.bytes = Buffer.byteLength(value.bytes);
    queue(responses(value)); render(<DevOpsInvestigationReport {...props(value.result)} />); await read();
    expect(await screen.findByRole('heading', { name: 'Findings' })).toBeInTheDocument();
    expect(screen.getByText('Unsafe link')).toBeInTheDocument();
    expect(screen.getByText('External image')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it.each([403, 404])('clears an already displayed report on revoked access (%s)', async status => {
    queue(responses()); render(<DevOpsInvestigationReport {...props()} />); await read();
    await screen.findByText('Configuration changed');
    fetchMock.mockResolvedValueOnce(new Response('{"detail":{"code":"not_found"}}', { status }));
    await read();
    expect(await screen.findByRole('alert')).toHaveTextContent('Report access is no longer available');
    expect(screen.queryByText('Configuration changed')).not.toBeInTheDocument();
    expect(onAccessDenied).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it.each(['tenant', 'revision', 'artifact', 'download path', 'expiry'])('rejects changed %s before reading report bytes', async change => {
    const value = fixture(); const replies = responses(value);
    if (change === 'tenant') replies[0] = new Response(JSON.stringify({ ...value.result, scope: { ...scope, tenant_id: 'foreign' } }), { headers: { ETag: '"1"' } });
    if (change === 'revision') replies[0] = new Response(JSON.stringify({ ...value.result, revision: 2 }), { headers: { ETag: '"2"' } });
    if (['artifact', 'download path', 'expiry'].includes(change)) replies[1] = new Response(JSON.stringify({
      artifact: change === 'artifact' ? { ...value.artifact, version: 'different' } : value.artifact,
      download_path: change === 'download path' ? 'https://untrusted.invalid/content' : base + '/artifacts/readback-result/content',
      expires_at: new Date(Date.now() + (change === 'expiry' ? -1000 : 60000)).toISOString(),
    }));
    queue(replies); render(<DevOpsInvestigationReport {...props(value.result)} />); await read();
    expect(await screen.findByRole('alert')).toHaveTextContent(/changed|expired/);
    expect(screen.queryByText('Configuration changed')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(['tenant', 'revision'].includes(change) ? 1 : 2);
  });

  it.each(['type', 'header', 'length', 'digest'])('rejects invalid report %s without displaying evidence', async change => {
    const value = fixture(); const replies = responses(value);
    const body = change === 'length' ? value.bytes + ' ' : change === 'digest' ? value.bytes.replace('Configuration changed', 'Configuration forgery') : value.bytes;
    replies[2] = new Response(body, { headers: { 'Content-Type': change === 'type' ? 'text/html' : 'application/json',
      'X-Adp-Content-Sha256': change === 'header' ? '0'.repeat(64) : value.artifact.sha256 } });
    queue(replies); render(<DevOpsInvestigationReport {...props(value.result)} />); await read();
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be read and verified');
    expect(screen.getByRole('alert')).toHaveTextContent(`Report artifact ${['type', 'header'].includes(change) ? 'metadata' : change} changed.`);
    expect(screen.queryByText('Configuration changed')).not.toBeInTheDocument();
  });

  it('rejects an intact artifact for a different native execution', async () => {
    const value = fixture('native'); value.evidence.executionId = 'foreign';
    value.bytes = JSON.stringify(value.evidence); value.artifact.sha256 = createHash('sha256').update(value.bytes).digest('hex');
    value.artifact.bytes = Buffer.byteLength(value.bytes);
    queue(responses(value)); render(<DevOpsInvestigationReport {...props(value.result)} />); await read();
    expect(await screen.findByRole('alert')).toHaveTextContent('does not match this investigation');
    expect(screen.queryByText('Investigate the timeout change')).not.toBeInTheDocument();
  });

  it.each(['result', 'scope'])('discards a pending response after the selected %s changes', async change => {
    const value = fixture(); const replies = responses(value); queue(replies.slice(0, 2));
    let resolve!: (value: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>(done => { resolve = done; }));
    const view = render(<DevOpsInvestigationReport {...props(value.result)} />);
    fireEvent.click(screen.getByRole('button', { name: 'Read investigation report' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const options = fetchMock.mock.calls[2][1];
    view.rerender(<DevOpsInvestigationReport {...props(value.result)} {...(change === 'result'
      ? { result: { ...value.result, result_id: 'different' } } : { scope: { ...scope, tenant_id: 'different' } })} />);
    expect(options.signal.aborted).toBe(true);
    await act(async () => { resolve(replies[2]); });
    expect(screen.queryByText('Configuration changed')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(onAccessDenied).not.toHaveBeenCalled();
  });

  it('removes visible report content immediately when project scope changes', async () => {
    queue(responses()); const view = render(<DevOpsInvestigationReport {...props()} />); await read();
    await screen.findByText('Configuration changed');
    view.rerender(<DevOpsInvestigationReport {...props()} projectId="different-project" />);
    expect(screen.queryByText('Configuration changed')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
