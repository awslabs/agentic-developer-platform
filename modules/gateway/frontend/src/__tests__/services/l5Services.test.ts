import { beforeEach, describe, expect, it, vi } from 'vitest';
import parity from '@/__tests__/fixtures/transform-client-parity.json';
import { listFinOpsRuns, listFinOpsResults, refreshSecurityProfile, getSecuritySourcePreflight } from '@/services/l5Services';
import { checkTransformConfiguration, getTransformConfiguration, getTransformConfigurationVersion, getTransformOptions, saveTransformConfiguration } from '@/services/l5Services';
import { getTransformArtifact, getTransformResult, getTransformRun, listTransformResults, listTransformRuns, mutateTransformRun, readTransformPatch, submitTransformRun } from '@/services/l5Services';
import { getFinOpsConfiguration, saveFinOpsConfiguration, startFinOpsBinding, submitFinOpsRun, getFinOpsRun, getFinOpsResult, getSecurityBinding, getSecurityConfiguration, getSecurityOptions, saveSecurityConfiguration, startSecurityBindingOperation, listSecurityRuns, getSecurityRun, submitSecurityRun, mutateSecurityRun, listSecurityResults, getSecurityResult, listSecurityFindings, listSecurityTriage, setSecurityTriage, compareSecurityResults, getSecurityPublication, retrySecurityPublication } from '@/services/l5Services';

const fetchMock = vi.fn();

describe('Security configuration transport', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    globalThis.fetch = fetchMock;
  });

  it('reads source size against the expected saved revision without a mutation request', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"status":"unknown"}'));
    await getSecuritySourcePreflight('product/a', 'project/b', 4);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/products/product%2Fa/projects/project%2Fb/services/security/configuration/source-preflight?configuration_revision=4');
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'GET' });
    expect(fetchMock.mock.calls[0][1].body).toBeUndefined();
  });

  it('refreshes only the reviewed Profile reference with a retained key and exact configuration ETag', async () => {
    const profile = { tenant_id: 'tenant-one', project_id: 'project/b', source_connection_id: 'source-one', record_type: 'product', record_id: 'product/a', record_revision: '4' };
    fetchMock.mockResolvedValueOnce(new Response('{"revision":4}', { headers: { ETag: '"revision-four"' } }));
    await refreshSecurityProfile('product/a', 'project/b', profile, '"revision-three"', 'reviewed_profile_1234');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/products/product%2Fa/projects/project%2Fb/services/security/configuration/refresh');
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST', headers: expect.objectContaining({ 'If-Match': '"revision-three"', 'Idempotency-Key': 'reviewed_profile_1234' }), body: JSON.stringify({ profile_ref: profile }) });
  });

  it('keeps the exact quoted server ETag and never requests setup or a run', async () => {
    const first = { revision: 2, settings: { capabilities: ['threat_model'] } };
    const second = { ...first, revision: 3 };
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify(first), { headers: { ETag: '"revision-two"' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(second), { headers: { ETag: '"revision-three"' } }));
    const read = await getSecurityConfiguration('product/a', 'project/b');
    const saved = await saveSecurityConfiguration('product/a', 'project/b', { enabled: true }, read.etag, 'fixed_receipt_123456');
    expect(read).toMatchObject({ value: first, etag: '"revision-two"' });
    expect(saved).toMatchObject({ value: second, etag: '"revision-three"' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain('/products/product%2Fa/projects/project%2Fb/services/security/configuration');
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'PUT', headers: expect.objectContaining({ 'If-Match': '"revision-two"', 'Idempotency-Key': 'fixed_receipt_123456' }), body: '{"enabled":true}' });
  });

  it('attaches the manual challenge only to the actual conditional Security save', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"revision":3}', { headers: { ETag: '"three"' } }));
    await saveSecurityConfiguration('product', 'project', { region: 'us-west-2' }, '"two"', 'original_ui_receipt_1', 'manual-request-1');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'PUT', headers: expect.objectContaining({
      'If-Match': '"two"', 'Idempotency-Key': 'original_ui_receipt_1', 'X-ADP-Security-Evidence': 'manual-request-1',
    }) });
  });

  it('uses conditional creation and fails closed when a revision header is missing', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{}', { headers: { ETag: '"first"' } }))
      .mockResolvedValueOnce(new Response('{}'));
    await saveSecurityConfiguration('product', 'project', { enabled: false }, undefined, 'fixed_receipt_123456');
    expect(fetchMock.mock.calls[0][1].headers).toMatchObject({ 'If-None-Match': '*' });
    await expect(getSecurityConfiguration('product', 'project')).rejects.toThrow('revision token');
  });

  it('separates the binding token from the operation token for conditional lifecycle actions', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"binding_id":"binding-one","revision":2}', { headers: { ETag: '"binding-two"' } }))
      .mockResolvedValueOnce(new Response('{"operation_id":"operation-one","state":"accepted"}', { headers: { ETag: '"operation-one"' } }));
    const binding = await getSecurityBinding('product', 'project', 'binding-one');
    const result = await startSecurityBindingOperation('product', 'project', {
      action: 'suspend', configuration_revision: 4, binding_id: binding.value.binding_id,
    }, binding.etag, 'fixed_receipt_123456');
    expect(fetchMock.mock.calls[1][1].headers).toMatchObject({ 'If-Match': '"binding-two"', 'Idempotency-Key': 'fixed_receipt_123456' });
    expect(result.etag).toBe('"operation-one"');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('uses the scoped run API with explicit admission and reasoned recovery receipts', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"items":[],"next_cursor":"run-one"}'))
      .mockResolvedValueOnce(new Response('{"run_id":"run-one","state":"partial"}', { headers: { ETag: '"run-two"' } }))
      .mockResolvedValueOnce(new Response('{"run_id":"run-two","state":"admitted"}', { headers: { ETag: '"run-one"' } }))
      .mockResolvedValueOnce(new Response('{"run_id":"run-one","state":"cancel_requested"}', { headers: { ETag: '"run-three"' } }));
    await listSecurityRuns('product/a', 'project/a', 'run+one');
    const current = await getSecurityRun('product/a', 'project/a', 'run-one');
    expect(current.etag).toBe('"run-two"');
    await submitSecurityRun('product/a', 'project/a', { configuration_revision: 4, binding_id: 'binding-one', capability: 'threat_model', sources: [] }, 'fixed_receipt_123456');
    await mutateSecurityRun('product/a', 'project/a', 'run-one', 'cancel', 'User cancelled assessment', 'fixed_receipt_654321');
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      expect.stringContaining('/products/product%2Fa/projects/project%2Fa/services/security/runs?cursor=run%2Bone'),
      expect.stringContaining('/runs/run-one'),
      expect.stringContaining('/services/security/runs'),
      expect.stringContaining('/runs/run-one/cancel'),
    ]);
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'fixed_receipt_123456' }),
      body: JSON.stringify({ configuration_revision: 4, binding_id: 'binding-one', capability: 'threat_model', sources: [] }) });
    expect(fetchMock.mock.calls[3][1]).toMatchObject({ method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'fixed_receipt_654321' }), body: '{"reason":"User cancelled assessment"}' });
  });

  it('preserves asynchronous Security preparation details and sends a cancellable original receipt', async () => {
    const controller = new AbortController();
    const detail = { code: 'source_capture_preparing', stage: 'capturing', retry_after_seconds: 5 };
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail }), { status: 503, headers: { 'Retry-After': '5' } }));
    await expect(submitSecurityRun('product', 'project', { configuration_revision: 4, binding_id: 'binding-one', capability: 'code_review', sources: [] },
      'fixed_receipt_123456', controller.signal)).rejects.toMatchObject({ status: 503, detail });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ signal: controller.signal, headers: expect.objectContaining({ 'Idempotency-Key': 'fixed_receipt_123456' }) });
  });

  it('preserves response status on conflict and reads only authorized options', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"detail":{"code":"stale_revision"}}', { status: 412 }))
      .mockResolvedValueOnce(new Response('{"connections":[],"sources":[],"regions":[],"capabilities":[],"unavailable_reasons":[]}'));
    await expect(saveSecurityConfiguration('product', 'project', { enabled: true }, '"stale"', 'fixed_receipt_123456')).rejects.toMatchObject({ status: 412 });
    await getSecurityOptions('product', 'project');
    expect(fetchMock.mock.calls[1][0]).toContain('/configuration/options');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
  it('scopes result, finding, triage, comparison and publication requests with exact revision and receipt', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"items":[]}'))
      .mockResolvedValueOnce(new Response('{"result_id":"result-one"}'))
      .mockResolvedValueOnce(new Response('{"items":[]}'))
      .mockResolvedValueOnce(new Response('{"items":[]}', { headers: { ETag: '"triage-empty"' } }))
      .mockResolvedValueOnce(new Response('{"finding_id":"finding-one","revision":1}', { headers: { ETag: '"triage-one"' } }))
      .mockResolvedValueOnce(new Response('{"comparability":"partial","reasons":[]}'))
      .mockResolvedValueOnce(new Response('{"publication_id":"publication-one"}'))
      .mockResolvedValueOnce(new Response('{"publication_id":"publication-one","state":"retry_wait"}'));
    await listSecurityResults('product/a', 'project/a', 'page+one');
    await getSecurityResult('product/a', 'project/a', 'result/one');
    await listSecurityFindings('product/a', 'project/a', 'result/one');
    const head = await listSecurityTriage('product/a', 'project/a', 'result/one', 'finding/one');
    await setSecurityTriage('product/a', 'project/a', 'result/one', 'finding/one', { decision: 'open', reason: 'Needs evidence' }, head.etag, 'fixed_receipt_123456');
    await compareSecurityResults('product/a', 'project/a', 'baseline', 'result/one');
    await getSecurityPublication('product/a', 'project/a', 'publication/one');
    await retrySecurityPublication('product/a', 'project/a', 'publication/one', 'Resume publication', 'fixed_receipt_654321');
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      expect.stringContaining('/products/product%2Fa/projects/project%2Fa/services/security/results?cursor=page%2Bone'),
      expect.stringContaining('/results/result%2Fone'), expect.stringContaining('/results/result%2Fone/findings'),
      expect.stringContaining('/findings/finding%2Fone/triage'), expect.stringContaining('/findings/finding%2Fone/triage'),
      expect.stringContaining('/services/security/comparisons'), expect.stringContaining('/publications/publication%2Fone'),
      expect.stringContaining('/publications/publication%2Fone/retry'),
    ]);
    expect(fetchMock.mock.calls[4][1]).toMatchObject({ method: 'PUT', headers: expect.objectContaining({ 'If-Match': '"triage-empty"', 'Idempotency-Key': 'fixed_receipt_123456' }), body: '{"decision":"open","reason":"Needs evidence"}' });
    expect(fetchMock.mock.calls[5][1]).toMatchObject({ method: 'POST', body: '{"baseline_result_id":"baseline","candidate_result_id":"result/one"}' });
    expect(fetchMock.mock.calls[7][1]).toMatchObject({ method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'fixed_receipt_654321' }), body: '{"reason":"Resume publication"}' });
  });

  it('refuses triage without the required current ETag', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"items":[]}'));
    await expect(listSecurityTriage('product', 'project', 'result', 'finding')).rejects.toThrow('revision token');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

});


describe('FinOps scoped Product transport', () => {
  beforeEach(() => { vi.resetAllMocks(); globalThis.fetch = fetchMock; });

  it('preserves opaque inquiry and result cursors on scoped page reads', async () => {
    const cursor = 'page/next+cursor=';
    for (const [list, path] of [[listFinOpsRuns, 'runs'], [listFinOpsResults, 'results']] as const) {
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ items: [], next_cursor: cursor })));
      const first = await list('product/a', 'project/b');
      expect(first.next_cursor).toBe(cursor);
      expect(fetchMock).toHaveBeenLastCalledWith(
        expect.stringContaining(`/products/product%2Fa/projects/project%2Fb/services/finops/${path}`), expect.anything());
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ items: [] })));
      await list('product/a', 'project/b', first.next_cursor);
      expect(fetchMock).toHaveBeenLastCalledWith(
        expect.stringContaining(`/services/finops/${path}?cursor=page%2Fnext%2Bcursor%3D`), expect.anything());
    }
  });

  it('retains server status and code on scoped FinOps failures', async () => {
    for (const [status, code] of [[404, 'not_found'], [412, 'stale_revision']] as const) {
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code, message: 'private-provider-secret' } }), { status }));
      await expect(getFinOpsConfiguration('product/a', 'project/b')).rejects.toMatchObject({
        status, detail: { code, message: 'private-provider-secret' },
      });
    }
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(Array(2).fill(
      expect.stringContaining('/products/product%2Fa/projects/project%2Fb/services/finops/configuration'),
    ));
  });

  it('keeps configuration, setup, admission and readback on the same route without rewriting the typed result', async () => {
    const scope = { tenant_id: 'tenant-example', product_id: 'product/a', project_id: 'project/b', relationship_revision: '1' };
    const configuration = { scope, revision: 3, settings: { service_id: 'finops', enabled: true, finops: { agent_mode: 'create' } } };
    const setup = { operation_id: 'operation-example', state: 'accepted' };
    const run = { scope, run_id: 'run-example', revision: 1, state: 'admitted' };
    const result = { scope, result_id: 'result-example', revision: 1, run_id: run.run_id,
      kind: 'finops_cost_inquiry', attribution: 'unknown',
      native: { status: 'outcome_unknown', answer_artifact: null, measure: { amount: null, coverage: 'unavailable' } },
      oracle: { pages: [], measure: { amount: '-12.3400', coverage: 'complete' } },
      comparison: { state: 'incomparable', delta: null, reason: 'native_unavailable' } };
    for (const [value, etag, status] of [
      [configuration, '"configuration-three"', 200], [configuration, '"configuration-four"', 200],
      [setup, '"setup-one"', 202], [run, '"run-one"', 202],
      [run, '"run-one"', 200], [result, '"result-one"', 200],
    ] as const) fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(value), { status, headers: { ETag: etag } }));
    const read = await getFinOpsConfiguration('product/a', 'project/b');
    const saved = await saveFinOpsConfiguration('product/a', 'project/b', configuration.settings, read.etag, 'configuration-key-123456');
    const operation = await startFinOpsBinding('product/a', 'project/b', { action: 'create', configuration_revision: saved.value.revision }, 'setup-key-example-123456');
    const input = { service_id: 'finops', capability: 'cost_inquiry', configuration_revision: 3, binding_id: 'binding-example',
      question: 'Original question?', query: { start: '2026-01-01', end: '2026-02-01', metric: 'UnblendedCost',
        granularity: 'MONTHLY', group_by: ['SERVICE'], currency: 'USD' } } as const;
    const accepted = await submitFinOpsRun('product/a', 'project/b', { ...input, query: { ...input.query, group_by: [...input.query.group_by] } }, 'inquiry-key-example-123456');
    const status = await getFinOpsRun('product/a', 'project/b', accepted.value.run_id);
    const answer = await getFinOpsResult('product/a', 'project/b', 'result-example');
    expect([read.etag, saved.etag, operation.etag, accepted.etag, status.etag, answer.etag]).toEqual([
      '"configuration-three"', '"configuration-four"', '"setup-one"', '"run-one"', '"run-one"', '"result-one"',
    ]);
    expect(answer.value).toEqual(result);
    expect(accepted.value.state).toBe('admitted');
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      ...['configuration', 'configuration', 'binding-operations', 'runs', 'runs/run-example', 'results/result-example']
        .map(segment => expect.stringContaining(`/products/product%2Fa/projects/project%2Fb/services/finops/${segment}`)),
    ]);
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'PUT', headers: expect.objectContaining({ 'If-Match': read.etag, 'Idempotency-Key': 'configuration-key-123456' }) });
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'setup-key-example-123456' }) });
    expect(fetchMock.mock.calls[3][1]).toMatchObject({ method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'inquiry-key-example-123456' }), body: JSON.stringify(input) });
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });
});


describe('Transform configuration transport', () => {
  beforeEach(() => { vi.resetAllMocks(); globalThis.fetch = fetchMock; });
  it('checks current readiness through the scoped server without submitting work', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"ready":true,"gaps":[]}'));
    expect(await checkTransformConfiguration('product/a', 'project/b')).toEqual({ ready: true, gaps: [] });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain('/products/product%2Fa/projects/project%2Fb/services/transform/configuration/check');
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST' });
  });
  it('retains the quoted ETag for scoped options, history and conditional save', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"service_id":"transform"}'))
      .mockResolvedValueOnce(new Response('{"revision":2}', { headers: { ETag: '"revision-two"' } }))
      .mockResolvedValueOnce(new Response('{"revision":1}', { headers: { ETag: '"revision-one"' } }))
      .mockResolvedValueOnce(new Response('{"revision":3}', { headers: { ETag: '"revision-three"' } }));
    await getTransformOptions('product/a', 'project/b');
    const current = await getTransformConfiguration('product/a', 'project/b');
    const previous = await getTransformConfigurationVersion('product/a', 'project/b', 1);
    await saveTransformConfiguration('product/a', 'project/b', { service_id: 'transform', capabilities: ['refactor'], enabled: false, transform: {} as never }, current.etag, 'receipt-example-123456');
    expect(previous.etag).toBe('"revision-one"');
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual([
      expect.stringContaining('/products/product%2Fa/projects/project%2Fb/services/transform/configuration/options'),
      expect.stringContaining('/products/product%2Fa/projects/project%2Fb/services/transform/configuration'),
      expect.stringContaining('/products/product%2Fa/projects/project%2Fb/services/transform/configuration/versions/1'),
      expect.stringContaining('/products/product%2Fa/projects/project%2Fb/services/transform/configuration'),
    ]);
    expect(fetchMock.mock.calls[3][1]).toMatchObject({ method: 'PUT', headers: expect.objectContaining({ 'If-Match': '"revision-two"', 'Idempotency-Key': 'receipt-example-123456' }) });
  });
});


describe('Transform run and evidence transport', () => {
  beforeEach(() => { vi.resetAllMocks(); globalThis.fetch = fetchMock; });
  it('keeps each opaque page cursor scoped and encoded', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"items":[],"next_cursor":"runs-next"}'))
      .mockResolvedValueOnce(new Response('{"items":[],"next_cursor":"results-next"}'));
    expect(await listTransformRuns('product/a', 'project/b', 'run/next+page')).toEqual({ items: [], next_cursor: 'runs-next' });
    expect(await listTransformResults('product/a', 'project/b', 'result/next+page')).toEqual({ items: [], next_cursor: 'results-next' });
    expect(fetchMock.mock.calls[0][0]).toContain('/products/product%2Fa/projects/project%2Fb/services/transform/runs?cursor=run%2Fnext%2Bpage');
    expect(fetchMock.mock.calls[1][0]).toContain('/products/product%2Fa/projects/project%2Fb/services/transform/results?cursor=result%2Fnext%2Bpage');
  });
  it('reads the shared pinned run and independent failing test evidence without promoting unknown to passed', async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(parity.run), { headers: { ETag: '"2"' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(parity.result), { headers: { ETag: '"3"' } }));
    const run = await getTransformRun('product-example', 'project-example', parity.run.run_id);
    const result = await getTransformResult('product-example', 'project-example', parity.result.result_id);
    expect(run).toEqual({ value: parity.run, etag: '"2"' });
    expect(result).toEqual({ value: parity.result, etag: '"3"' });
    expect(result.value.tests_after[0].status).toBe('failed');
    expect(run.value.provider_state).toBe('unknown');
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      expect.stringContaining('/products/product-example/projects/project-example/services/transform/runs/run-one'),
      expect.stringContaining('/products/product-example/projects/project-example/services/transform/results/result-example'),
    ]);
  });
  it('preserves status, ETag and idempotency for accepted runs and read-only results', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"run_id":"run-one"}', { status: 202, headers: { ETag: '"run-one"' } }))
      .mockResolvedValueOnce(new Response('{"run_id":"run-one"}', { headers: { ETag: '"run-two"' } }))
      .mockResolvedValueOnce(new Response('{"run_id":"run-one"}', { status: 202, headers: { ETag: '"run-three"' } }))
      .mockResolvedValueOnce(new Response('{"items":[]}'))
      .mockResolvedValueOnce(new Response('{"items":[]}'))
      .mockResolvedValueOnce(new Response('{"result_id":"result-one"}', { headers: { ETag: '"result-one"' } }));
    const input = { service_id: 'transform' as const, configuration_revision: 3, binding_id: 'binding-one', capability: 'refactor' as const, sources: [], transform_input: { goal: { kind: 'refactor' as const, summary: 'Improve structure' } } };
    const accepted = await submitTransformRun('product/a', 'project/b', input, 'submit-receipt-123456');
    const read = await getTransformRun('product/a', 'project/b', accepted.value.run_id);
    const cancelled = await mutateTransformRun('product/a', 'project/b', read.value.run_id, 'cancel', 'stop', 'cancel-receipt-123456');
    await listTransformRuns('product/a', 'project/b');
    await listTransformResults('product/a', 'project/b');
    const result = await getTransformResult('product/a', 'project/b', 'result-one');
    expect([accepted.etag, read.etag, cancelled.etag, result.etag]).toEqual(['"run-one"', '"run-two"', '"run-three"', '"result-one"']);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'submit-receipt-123456' }), body: JSON.stringify(input) });
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'cancel-receipt-123456' }), body: '{"reason":"stop"}' });
    expect(fetchMock.mock.calls[5][0]).toContain('/products/product%2Fa/projects/project%2Fb/services/transform/results/result-one');
  });
  it('does not turn unavailable readback into a passed result', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{"detail":{"code":"transform_unavailable"}}', { status: 503 }));
    await expect(listTransformResults('product', 'project')).rejects.toMatchObject({ status: 503, detail: { code: 'transform_unavailable' } });
  });
  it('requires matching artifact identity and a browser-verified SHA-256 before showing patch bytes', async () => {
    const digest = 'a'.repeat(64);
    const artifact = { artifact_id: 'artifact-one', sha256: digest };
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ artifact }), { headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response('original patch', { headers: { 'Content-Type': 'text/x-diff', 'X-Adp-Content-Sha256': digest } }));
    await getTransformArtifact('product', 'project', 'artifact-one');
    vi.stubGlobal('crypto', { subtle: { digest: vi.fn().mockResolvedValue(Uint8Array.from(Array(32).fill(170)).buffer) } });
    expect(await readTransformPatch('product', 'project', 'artifact-one', digest)).toBe('original patch');
    vi.unstubAllGlobals();
    fetchMock.mockResolvedValueOnce(new Response('forged patch', { headers: { 'Content-Type': 'text/x-diff', 'X-Adp-Content-Sha256': 'b'.repeat(64) } }));
    await expect(readTransformPatch('product', 'project', 'artifact-one', digest)).rejects.toThrow('metadata');
  });
});

describe('Well-Architected scoped client parity', () => {
  beforeEach(() => { vi.resetAllMocks(); globalThis.fetch = fetchMock; });
  it('reads result publication debt from the same scoped Product API', async () => {
    const { getWellArchitectedPublicationStatus } = await import('@/services/l5Services');
    const status = { result_id: 'result-one', revision: 3,
      consumers: [{ destination: 'product-context', state: 'unavailable', projection_verified_on_ack: false }] };
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(status)));
    expect(await getWellArchitectedPublicationStatus('product-one', 'project-one', 'result-one')).toEqual(status);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining(
      '/products/product-one/projects/project-one/services/well_architected/results/result-one/publication-status'), expect.anything());
  });
  it('reads Well-Architected comparability from the shared Product API', async () => {
    const { compareWellArchitectedResults } = await import('@/services/l5Services');
    const fixture = (await import('@/__tests__/fixtures/well-architected-client-parity.json')).default;
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(fixture.states.comparison)));
    expect(await compareWellArchitectedResults('product-one', 'project-one', 'result-previous', 'result-one'))
      .toEqual(fixture.states.comparison);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining(
      '/products/product-one/projects/project-one/services/well_architected/comparisons'),
    expect.objectContaining({ method: 'POST', body: JSON.stringify({ baseline_result_id: 'result-previous', candidate_result_id: 'result-one' }) }));
  });
  it('uses the shared Product review endpoints, revisions and replay keys', async () => {
    const { getWellArchitectedConfiguration, saveWellArchitectedConfiguration, submitWellArchitectedRun,
      getWellArchitectedRun, getWellArchitectedResult } = await import('@/services/l5Services');
    const fixture = (await import('@/__tests__/fixtures/well-architected-client-parity.json')).default;
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(fixture.configuration), { headers: { ETag: '"hash-one"' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(fixture.configuration), { headers: { ETag: '"hash-one"' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(fixture.run), { status: 202, headers: { ETag: '"2"' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(fixture.run), { status: 202, headers: { ETag: '"2"' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(fixture.run), { headers: { ETag: '"2"' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(fixture.result), { headers: { ETag: '"3"' } }));
    const read = await getWellArchitectedConfiguration('product-one', 'project-one');
    const choices = { service_id: 'well_architected' as const, capabilities: ['architecture_review'] as ['architecture_review'],
      enabled: false, well_architected: { review_mode: 'source_only' as const, pillars: ['SECURITY'] as ['SECURITY'], goal_refs: [] },
      triggers: [{ trigger_id: 'daily-one', kind: 'schedule', enabled: false, capability: 'architecture_review',
        binding_id: fixture.run_input.binding_id, source_refs: fixture.run_input.sources, debounce_seconds: 0, schedule: '30 2 * * *' }] };
    const saved = await saveWellArchitectedConfiguration('product-one', 'project-one', choices, read.etag, fixture.save_key);
    const accepted = await submitWellArchitectedRun('product-one', 'project-one', fixture.run_input.binding_id,
      fixture.configuration.revision, fixture.run_input.sources, fixture.run_key);
    const replayed = await submitWellArchitectedRun('product-one', 'project-one', fixture.run_input.binding_id,
      fixture.configuration.revision, fixture.run_input.sources, fixture.run_key);
    const run = await getWellArchitectedRun('product-one', 'project-one', accepted.value.run_id);
    const result = await getWellArchitectedResult('product-one', 'project-one', fixture.result.result_id);
    expect([read, saved, accepted, replayed, run, result]).toEqual([
      { value: fixture.configuration, etag: '"hash-one"' }, { value: fixture.configuration, etag: '"hash-one"' },
      { value: fixture.run, etag: '"2"' }, { value: fixture.run, etag: '"2"' },
      { value: fixture.run, etag: '"2"' }, { value: fixture.result, etag: '"3"' },
    ]);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      'configuration', 'configuration', 'runs', 'runs', 'runs/run-one', 'results/result-one'
    ].map(segment => expect.stringContaining(`/products/product-one/projects/project-one/services/well_architected/${segment}`)));
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: 'PUT', headers: expect.objectContaining({ 'If-Match': read.etag, 'Idempotency-Key': fixture.save_key }), body: JSON.stringify(choices) });
    for (const index of [2, 3]) expect(fetchMock.mock.calls[index][1]).toMatchObject({ method: 'POST',
      headers: expect.objectContaining({ 'Idempotency-Key': fixture.run_key }), body: JSON.stringify(fixture.run_input) });
  });
});

describe('Well-Architected setup receipt parity', () => {
  beforeEach(() => { vi.resetAllMocks(); globalThis.fetch = fetchMock; });
  it('uses the shared scoped binding operation and preserves its receipt', async () => {
    const { startWellArchitectedBinding, getWellArchitectedBindingOperation } = await import('@/services/l5Services');
    const fixture = (await import('@/__tests__/fixtures/well-architected-client-parity.json')).default;
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(fixture.setup), { status: 202, headers: { ETag: '"1"' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(fixture.setup)));
    const receipt = await startWellArchitectedBinding('product-one', 'project-one', fixture.configuration.revision, fixture.setup_key);
    const read = await getWellArchitectedBindingOperation('product-one', 'project-one', receipt.value.operation_id);
    expect(receipt).toEqual({ value: fixture.setup, etag: '"1"' });
    expect(read).toEqual(fixture.setup);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      expect.stringContaining('/products/product-one/projects/project-one/services/well_architected/binding-operations'),
      expect.stringContaining('/products/product-one/projects/project-one/services/well_architected/binding-operations/setup-one'),
    ]);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'POST',
      headers: expect.objectContaining({ 'Idempotency-Key': fixture.setup_key }),
      body: JSON.stringify({ action: 'create', configuration_revision: fixture.configuration.revision }) });
  });
});

describe('Well-Architected provider selection', () => {
  beforeEach(() => { vi.resetAllMocks(); globalThis.fetch = fetchMock; });
  it('posts only the explicit resources on the scoped selection endpoint', async () => {
    const { validateWellArchitectedProviderSelection } = await import('@/services/l5Services');
    const connection = { tenant_id: 'tenant-one', project_id: 'project/one', source_connection_id: 'connection-one',
      record_type: 'connection', record_id: 'connection-one', record_revision: '7' };
    const input = { connection_ref: connection, region: 'eu-west-1', execution_role_arn: 'arn:aws:iam::123456789012:role/source-only', source_bucket: 'example-private-sources' };
    const response = { provider_contacted: true, native_qualified: false,
      execution_role_ref: { ...connection, record_type: 'role', record_id: input.execution_role_arn, record_revision: 'a'.repeat(64) },
      source_store_ref: { ...connection, record_type: 'artifact_store', record_id: input.source_bucket, record_revision: 'b'.repeat(64) } };
    fetchMock.mockResolvedValue(new Response(JSON.stringify(response)));
    expect(await validateWellArchitectedProviderSelection('product/one', 'project/one', input)).toEqual(response);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('/products/product%2Fone/projects/project%2Fone/services/well_architected/configuration/provider-selection'),
      expect.objectContaining({ method: 'POST', redirect: 'error', body: JSON.stringify(input) }));
  });
});

describe('DevOps and Well-Architected paged readback transport', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.stubGlobal('fetch', fetchMock); });
  it('preserves opaque cursors and encodes them within each exact service scope', async () => {
    const api = await import('@/services/l5Services');
    const cursor = 'page/next+cursor=';
    const lists = [
      [api.listDevOpsRuns, 'devops/runs'],
      [api.listDevOpsResults, 'devops/results'],
      [api.listWellArchitectedRuns, 'well_architected/runs'],
      [api.listWellArchitectedResults, 'well_architected/results'],
    ] as const;
    for (const [list, path] of lists) {
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ items: [], next_cursor: cursor })));
      const first = await list('product/a', 'project/b');
      expect(first.next_cursor).toBe(cursor);
      fetchMock.mockResolvedValueOnce(new Response('{"items":[]}'));
      await list('product/a', 'project/b', first.next_cursor);
      expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining(
        `/products/product%2Fa/projects/project%2Fb/services/${path}?cursor=page%2Fnext%2Bcursor%3D`), expect.anything());
    }
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ items: [], next_cursor: cursor })));
    expect((await api.listDevOpsFindings('product/a', 'project/b', 'result/a')).next_cursor).toBe(cursor);
    fetchMock.mockResolvedValueOnce(new Response('{"items":[]}'));
    await api.listDevOpsFindings('product/a', 'project/b', 'result/a', cursor);
    expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining(
      '/products/product%2Fa/projects/project%2Fb/services/devops/results/result%2Fa/findings?cursor=page%2Fnext%2Bcursor%3D'), expect.anything());
  });
});
