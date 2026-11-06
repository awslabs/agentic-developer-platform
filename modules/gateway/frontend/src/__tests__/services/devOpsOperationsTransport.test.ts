import { beforeEach, describe, expect, it, vi } from 'vitest';
import { reconcileDevOpsRun } from '@/services/l5Services';
import { configureDevOpsPrevention, getDevOpsBinding, getDevOpsPrevention, getDevOpsPublication, launchDevOpsBrowser, listDevOpsBindings, listDevOpsEvents, listDevOpsFindings, listDevOpsResults, listDevOpsRuns, startDevOpsBindingOperation, submitDevOpsRun } from '@/services/l5Services';

const fetchMock = vi.fn();
const base = '/products/product%2Fa/projects/project%2Fb/services/devops';

describe('DevOps operation transport', () => {
  beforeEach(() => { vi.resetAllMocks(); globalThis.fetch = fetchMock; });

  it('recovers the exact existing run with the retained receipt', async () => {
    fetchMock.mockResolvedValue(new Response('{"run_id":"run/one","revision":2}', { headers: { ETag: '"2"' } }));
    await reconcileDevOpsRun('product/a', 'project/b', 'run/one', 'retained-recovery-receipt');
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining(`${base}/runs/run%2Fone/reconcile`), expect.objectContaining({
      method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'retained-recovery-receipt' }),
      body: JSON.stringify({ reason: 'Read the existing investigation and collect its report without starting another investigation.' }),
    }));
  });

  it('pins fresh read-only report authorization to the displayed run and native task', async () => {
    const native = { tenant_id: 'tenant', project_id: 'project/b', source_connection_id: 'connection',
      record_type: 'native_job', record_id: 'execution-one', record_revision: 'task-one' };
    fetchMock.mockResolvedValue(new Response('{"run_id":"run/one","revision":8}', { headers: { ETag: '"8"' } }));
    await reconcileDevOpsRun('product/a', 'project/b', 'run/one', 'retained-recovery-receipt', { revision: 7, native_job_ref: native });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      reason: 'Read the existing investigation and collect its report without starting another investigation.',
      authorize_readback: true, expected_revision: 7, native_job_ref: native,
    });
  });

  it('uses shared scoped readback paths and an authenticated browser-only POST', async () => {
    fetchMock.mockImplementation(async (_: string, request: RequestInit) => new Response('{}', {
      headers: request.method === 'GET' ? { ETag: '"binding"' } : {},
    }));
    await listDevOpsBindings('product/a', 'project/b');
    await getDevOpsBinding('product/a', 'project/b', 'binding/one');
    await getDevOpsPrevention('product/a', 'project/b', 'binding/one');
    await listDevOpsEvents('product/a', 'project/b', 'binding/one');
    await listDevOpsRuns('product/a', 'project/b');
    await listDevOpsResults('product/a', 'project/b');
    await listDevOpsFindings('product/a', 'project/b', 'result/one');
    await getDevOpsPublication('product/a', 'project/b', 'publication/one');
    await launchDevOpsBrowser('product/a', 'project/b', 'binding/one', 4);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      expect.stringContaining(`${base}/bindings`), expect.stringContaining(`${base}/bindings/binding%2Fone`),
      expect.stringContaining(`${base}/bindings/binding%2Fone/prevention`), expect.stringContaining(`${base}/bindings/binding%2Fone/events`),
      expect.stringContaining(`${base}/runs`), expect.stringContaining(`${base}/results`),
      expect.stringContaining(`${base}/results/result%2Fone/findings`), expect.stringContaining(`${base}/publications/publication%2Fone`),
      expect.stringContaining(`${base}/bindings/binding%2Fone/browser-launch`),
    ]);
    expect(fetchMock.mock.calls[8][1]).toMatchObject({ method: 'POST', body: '{"configuration_revision":4}' });
  });

  it('pins binding, prevention and manual run mutations to explicit revisions and receipts', async () => {
    fetchMock.mockImplementation(async () => new Response('{}', { headers: { ETag: '"next"' } }));
    await startDevOpsBindingOperation('product/a', 'project/b', { action: 'check', configuration_revision: 4, binding_id: 'binding/one' }, '"binding"', 'fixed_receipt_123456');
    await configureDevOpsPrevention('product/a', 'project/b', 'binding/one', 4, 'pause', '"binding"', 'fixed_receipt_123456');
    await submitDevOpsRun('product/a', 'project/b', {
      service_id: 'devops', configuration_revision: 4, binding_id: 'binding/one', capability: 'investigation',
      devops_input: { kind: 'investigation', question: 'Investigate', resource_refs: [], telemetry_refs: [] },
    }, 'fixed_receipt_123456');
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      expect.stringContaining(`${base}/binding-operations`), expect.stringContaining(`${base}/bindings/binding%2Fone/prevention`),
      expect.stringContaining(`${base}/runs`),
    ]);
    for (const [, request] of fetchMock.mock.calls) {
      expect(request).toMatchObject({ method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'fixed_receipt_123456' }) });
    }
    expect(fetchMock.mock.calls[0][1].headers).toMatchObject({ 'If-Match': '"binding"' });
    expect(fetchMock.mock.calls[1][1].headers).toMatchObject({ 'If-Match': '"binding"' });
    expect(fetchMock.mock.calls[2][1].body).toContain('"service_id":"devops"');
  });
});
