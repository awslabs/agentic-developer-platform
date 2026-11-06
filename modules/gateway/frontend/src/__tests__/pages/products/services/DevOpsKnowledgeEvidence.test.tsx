import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import DevOpsKnowledgeEvidence from '@/pages/products/services/DevOpsKnowledgeEvidence';
import { apiClient } from '@/services/api';
import type { KnowledgeEvidencePage, KnowledgeObservation, KnowledgePreparation, NativeArrival } from '@/services/devopsKnowledgeEvidence';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), request: vi.fn(), requestWithEtag: vi.fn() } }));
const selection = { tenant_id: 'tenant', product_id: 'product', project_id: 'project', configuration_id: 'configuration', configuration_revision: 3, binding_id: 'binding', binding_generation: 2 };
const base = '/v1/products/product/projects/project/services/devops';
const path = base + '/binding-operations/original/knowledge-evidence';
const source = 'a'.repeat(64);
const expires = '2100-01-01T00:00:00Z';
const nativeArrival: NativeArrival = { flowId: 'flow', challenge_id: 'arrival', service_id: 'devops', subject: 'native_ui', expires_at: expires, snapshot: { selection }, observation_receipt: { provenance: 'authenticated-human-observation', observed: { observation: 'displayed' } } };
const publication = { source_key: source, source_ref: { tenant_id: 'tenant', project_id: 'project', source_connection_id: 'connection', record_type: 'document', record_id: 'document', record_revision: 'v1' }, active: true, native_type: 'skill', metadata: { name: 'incident-policy' }, asset_id: 'asset', version: 1, source_expires_at: expires };
const prepared = { evidence_id: 'evidence', kind: 'prepared', actor_id: 'human', event_hash: 'b'.repeat(64), created_at: '2026-10-31T10:00:00Z', expires_at: expires, selection: { source_key: source, receipt: { metadata: publication.metadata }, native_type: 'skill' } };
const observed = { evidence_id: 'evidence', kind: 'observed', actor_id: 'human', event_hash: 'c'.repeat(64), created_at: '2026-10-31T10:01:00Z', source_key: source, preparation_hash: prepared.event_hash };
const scenario: KnowledgePreparation = { source_key: source, flow_id: 'flow', challenge_id: 'arrival', prompt: 'Which process should this team follow?', expected_behavior: 'Use the incident policy', source_excerpt: 'Require a second reviewer', reason: 'Verify the source changes the operational decision' };
const asset = { asset_id: 'asset', version: 1, observed_at: '2026-10-31T10:00:10Z', content: 'Require a second reviewer', metadata: { name: 'incident-policy' } };
const observation: KnowledgeObservation = { preparation_hash: prepared.event_hash, native_session_id: 'native-session', native_agent_type: 'CHAT', session_started_at: '2026-10-31T10:00:20Z', before: asset, after: { ...asset, observed_at: '2026-10-31T10:00:40Z' }, transcript: [{ role: 'user', text: scenario.prompt }, { role: 'assistant', text: 'Use incident-policy: require a second reviewer.' }], response_quote: 'require a second reviewer', source_citation_quote: 'incident-policy', skill_invocation_quote: 'Use incident-policy', explanation: 'The agent followed the selected source when making the operational decision.' };
let page: KnowledgeEvidencePage;
const onAccessDenied = vi.fn();
beforeEach(() => {
  vi.resetAllMocks();
  page = { operation_id: 'original', revision: 5, state: 'waiting', selection, publications: [publication], items: [], eligibility: { can_prepare: true, can_review: true, blocked_code: null } };
  vi.mocked(apiClient.requestWithEtag).mockImplementation(async () => ({ value: page, etag: `"${page.revision}"` }));
  vi.mocked(apiClient.request).mockResolvedValue({});
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint.includes('/knowledge-operations?')) return { items: [{ operation_id: 'original', binding_id: 'binding', state: 'waiting', revision: 5, configuration_revision: 3, binding_generation: 2 }], next_offset: null };
    if (endpoint.endsWith('/artifacts/prepared')) return { evidence_id: 'evidence', event_hash: prepared.event_hash, kind: 'prepared', artifact: scenario };
    if (endpoint.endsWith('/artifacts/observed')) return { evidence_id: 'evidence', event_hash: observed.event_hash, kind: 'observed', artifact: observation };
    return nativeArrival;
  });
});
function mount(props = {}) { return render(<DevOpsKnowledgeEvidence selection={selection} disabled={false} nativeArrival={nativeArrival} onAccessDenied={onAccessDenied} {...props} />); }
async function load() {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh publication operations' })); });
  fireEvent.change(screen.getByLabelText('Knowledge publication operation'), { target: { value: 'original' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Read knowledge evidence' })); });
}
function change(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
async function prepare() {
  change('Published knowledge', source);
  change('Prompt you will send to DevOps', scenario.prompt);
  change('Expected source-dependent behavior', scenario.expected_behavior);
  change('Exact source excerpt supporting that expectation', scenario.source_excerpt);
  change('Why this scenario tests the selected knowledge', scenario.reason);
  await act(async () => { fireEvent.submit(screen.getByRole('button', { name: 'Prepare my observation' }).closest('form')!); });
}
it('discovers the original operation and named publication through scoped session APIs', async () => {
  mount(); await load();
  expect(apiClient.get).toHaveBeenCalledWith(base + '/knowledge-operations?binding_id=binding&offset=0&limit=50');
  expect(apiClient.requestWithEtag).toHaveBeenCalledWith(path);
  expect(screen.getByRole('option', { name: 'incident-policy — skill' })).toBeInTheDocument();
  expect(apiClient.request).not.toHaveBeenCalled();
});
it('prepares only the entered declaration using original selection, arrival, ETag and a retained key', async () => {
  mount(); await load(); await prepare();
  expect(apiClient.request).toHaveBeenCalledWith(path + '/prepare', { method: 'POST', headers: { 'If-Match': '"5"', 'Idempotency-Key': expect.any(String) }, body: scenario });
  expect(apiClient.requestWithEtag).toHaveBeenCalledTimes(2);
});
it('reads back uncertainty and retries the identical request and key', async () => {
  vi.mocked(apiClient.request).mockRejectedValueOnce(new Error('Response lost'));
  mount(); await load(); await prepare();
  const original = vi.mocked(apiClient.request).mock.calls[0];
  expect(apiClient.requestWithEtag).toHaveBeenCalledTimes(2);
  expect(screen.getByRole('button', { name: 'Prepare my observation' })).toBeDisabled();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Retry saved evidence request' })); });
  expect(vi.mocked(apiClient.request).mock.calls[1]).toEqual(original);
});
it('keeps retry disabled if uncertain-request readback also fails', async () => {
  mount(); await load();
  vi.mocked(apiClient.request).mockRejectedValueOnce(new Error('Response lost'));
  vi.mocked(apiClient.requestWithEtag).mockRejectedValueOnce(new Error('Read unavailable'));
  await prepare();
  expect(screen.getByRole('button', { name: 'Retry saved evidence request' })).toBeDisabled();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Read knowledge evidence' })); });
  expect(screen.getByRole('button', { name: 'Retry saved evidence request' })).toBeEnabled();
});
it('clears evidence and saved retries when access is revoked during uncertainty readback', async () => {
  mount(); await load();
  vi.mocked(apiClient.request).mockRejectedValueOnce(new Error('Response lost'));
  vi.mocked(apiClient.requestWithEtag).mockRejectedValueOnce({ status: 403, detail: { code: 'knowledge_evidence_source_access_revoked' } });
  await prepare();
  expect(onAccessDenied).toHaveBeenCalledOnce();
  expect(screen.queryByRole('button', { name: 'Retry saved evidence request' })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Published knowledge')).not.toBeInTheDocument();
});
it('paginates the selected binding operation history without choosing another scope', async () => {
  vi.mocked(apiClient.get).mockResolvedValueOnce({ items: [{ operation_id: 'original', binding_id: 'binding', state: 'waiting', configuration_revision: 3, binding_generation: 2 }], next_offset: 50 }).mockResolvedValueOnce({ items: [{ operation_id: 'older', binding_id: 'binding', state: 'succeeded', configuration_revision: 2, binding_generation: 1 }], next_offset: null });
  mount();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh publication operations' })); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'More publication operations' })); });
  expect(apiClient.get).toHaveBeenLastCalledWith(base + '/knowledge-operations?binding_id=binding&offset=50&limit=50');
  expect(screen.getByRole('option', { name: 'Configuration 2, binding generation 1 — succeeded' })).toBeInTheDocument();
});
it('requires actual readbacks, transcript and personal confirmation without prefilled observed data', async () => {
  page.items = [prepared]; mount(); await load();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Read scenario and evidence' })); });
  expect(screen.getByLabelText('Before full Asset content')).toHaveValue('');
  expect(screen.getByLabelText('Turn 1 text')).toHaveValue('');
  expect(screen.getByRole('button', { name: 'Record my knowledge observation' })).toBeDisabled();
  change('Native conversation ID', observation.native_session_id); change('Agent used', 'CHAT'); change('Conversation start time (include time zone)', observation.session_started_at);
  for (const [name, value] of [['Before', observation.before], ['After', observation.after]] as const) {
    change(`${name} Asset ID`, value.asset_id); change(`${name} Asset version`, String(value.version)); change(`${name} readback time (include time zone)`, value.observed_at); change(`${name} full Asset content`, value.content); change(`${name} Asset metadata (JSON)`, JSON.stringify(value.metadata));
  }
  change('Turn 1 text', observation.transcript[0].text); change('Turn 2 text', observation.transcript[1].text);
  change('Exact response showing the expected behavior', observation.response_quote); change('Exact source citation from the response', observation.source_citation_quote); change('Exact skill invocation from the response', observation.skill_invocation_quote!); change('How the source affected this response', observation.explanation);
  fireEvent.click(screen.getByLabelText('I personally observed this session and these readbacks.'));
  await act(async () => { fireEvent.submit(screen.getByRole('button', { name: 'Record my knowledge observation' }).closest('form')!); });
  expect(apiClient.request).toHaveBeenCalledWith(path + '/evidence/observe', { method: 'POST', headers: {}, body: observation });
});
it('requires reading retained artifacts and explicit semantic review before completing the original operation', async () => {
  page.items = [prepared, observed]; mount(); await load();
  expect(screen.getByRole('button', { name: 'Complete knowledge review' })).toBeDisabled();
  change('Observation to review for incident-policy', 'evidence');
  expect(screen.getByLabelText('I reviewed the actual response and its source-dependent behavior.')).toBeDisabled();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Read this review evidence' })); });
  expect(screen.getByText(observation.transcript[1].text)).toBeInTheDocument();
  const explanation = 'I reviewed the actual source-dependent choice in this native conversation.';
  change('Your review of incident-policy', explanation);
  fireEvent.click(screen.getByLabelText('I reviewed the actual response and its source-dependent behavior.'));
  vi.mocked(apiClient.request).mockImplementationOnce(async () => { page = { ...page, state: 'succeeded', revision: 6, eligibility: { can_prepare: false, can_review: false, blocked_code: null } }; return {}; });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Complete knowledge review' })); });
  expect(apiClient.request).toHaveBeenCalledWith(path + '/complete', { method: 'POST', headers: { 'If-Match': '"5"', 'Idempotency-Key': expect.any(String) }, body: { reviews: [{ evidence_id: 'evidence', observation_hash: observed.event_hash, source_effect_explanation: explanation }] } });
  expect(screen.getByText(/Knowledge review is complete/)).toBeInTheDocument();
});
it.each(['disabled', 'expired source', 'inactive', 'revoked', 'expired arrival'])('prevents preparation when %s', async boundary => {
  if (boundary === 'expired source') page.publications = [{ ...publication, source_expires_at: '2000-01-01T00:00:00Z' }];
  if (boundary === 'inactive') page.publications = [{ ...publication, active: false }];
  if (boundary === 'revoked') page.eligibility = { can_prepare: false, can_review: false, blocked_code: 'knowledge_evidence_source_access_revoked' };
  mount({ disabled: boundary === 'disabled', nativeArrival: boundary === 'expired arrival' ? { ...nativeArrival, expires_at: '2000-01-01T00:00:00Z' } : nativeArrival });
  await load(); change('Published knowledge', source);
  expect(screen.getByRole('button', { name: 'Prepare my observation' })).toBeDisabled();
  expect(apiClient.request).not.toHaveBeenCalled();
});
it('keeps a terminal abandoned operation read-only', async () => {
  page.state = 'abandoned'; mount(); await load(); change('Published knowledge', source);
  expect(screen.getByText(/This operation is read-only/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Prepare my observation' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Complete knowledge review' })).toBeDisabled();
});
it('rejects another binding before exposing evidence', async () => {
  page.selection = { ...selection, binding_id: 'foreign' }; mount(); await load();
  expect(screen.getByRole('alert')).toHaveTextContent('different binding');
  expect(screen.queryByLabelText('Published knowledge')).not.toBeInTheDocument();
  expect(apiClient.request).not.toHaveBeenCalled();
});
it.each(['configuration_revision', 'binding_generation'] as const)('reads retained history after %s changes without allowing writes', async field => {
  page.selection = { ...selection, [field]: selection[field] - 1 };
  page.items = [prepared, observed];
  mount(); await load(); change('Published knowledge', source);
  expect(screen.getByText(/This is retained history/)).toBeInTheDocument();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Read scenario and evidence' })); });
  expect(screen.getByText(observation.transcript[1].text)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Prepare my observation' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Complete knowledge review' })).toBeDisabled();
  expect(screen.getByText(/A current native arrival cannot authorize/)).toBeInTheDocument();
  expect(apiClient.request).not.toHaveBeenCalled();
});
it('blocks uncertain saved-request retries if readback resolves to historical scope', async () => {
  mount(); await load();
  vi.mocked(apiClient.request).mockImplementationOnce(async () => {
    page = { ...page, selection: { ...selection, configuration_revision: selection.configuration_revision - 1 } };
    throw new Error('Response lost');
  });
  await prepare();
  expect(screen.getByRole('button', { name: 'Retry saved evidence request' })).toBeDisabled();
  expect(apiClient.request).toHaveBeenCalledTimes(1);
});
it('clears private evidence when artifact access is revoked', async () => {
  page.items = [prepared]; mount(); await load();
  vi.mocked(apiClient.get).mockRejectedValueOnce({ status: 403, detail: { code: 'knowledge_evidence_source_access_revoked' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Read scenario and evidence' })); });
  expect(onAccessDenied).toHaveBeenCalledOnce();
  expect(screen.queryByText(scenario.source_excerpt)).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Published knowledge')).not.toBeInTheDocument();
});
it('requires fresh readback after a stale revision and keeps the server action message', async () => {
  vi.mocked(apiClient.request).mockRejectedValueOnce({ status: 412, detail: { next_action: 'Refresh the original operation after its revision changed.' } });
  mount(); await load(); await prepare();
  expect(screen.getByRole('alert')).toHaveTextContent('Refresh the original operation');
  expect(screen.queryByRole('button', { name: 'Prepare my observation' })).not.toBeInTheDocument();
});
it('ignores an old read response after switching binding scope', async () => {
  const view = mount();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh publication operations' })); });
  change('Knowledge publication operation', 'original');
  let resolve!: (value: { value: KnowledgeEvidencePage; etag: string }) => void;
  vi.mocked(apiClient.requestWithEtag).mockReturnValueOnce(new Promise(done => { resolve = done; }));
  fireEvent.click(screen.getByRole('button', { name: 'Read knowledge evidence' }));
  view.rerender(<DevOpsKnowledgeEvidence selection={{ ...selection, binding_id: 'another' }} disabled={false} onAccessDenied={onAccessDenied} />);
  await act(async () => { resolve({ value: page, etag: '"5"' }); });
  await waitFor(() => expect(screen.queryByLabelText('Published knowledge')).not.toBeInTheDocument());
});
