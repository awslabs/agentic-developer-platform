import { useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';
import { productReceipt } from '@/services/products';
import { knowledgeError, knowledgeEvidencePath, listKnowledgeOperations, readKnowledgeArtifact, readKnowledgeEvidence,
  type KnowledgeEvidence, type KnowledgeEvidencePage, type KnowledgeObservation, type KnowledgePreparation,
  type KnowledgeOperation, type KnowledgeSelection, type NativeArrival } from '@/services/devopsKnowledgeEvidence';
import { KnowledgeObservationForm, KnowledgePreparationForm } from './DevOpsKnowledgeForms';
import DevOpsKnowledgeAbandon from './DevOpsKnowledgeAbandon';

interface Props {
  selection: KnowledgeSelection; disabled: boolean; nativeArrival?: NativeArrival | null;
  operationId?: string; onAccessDenied: () => void;
}
interface SavedRequest { path: string; body: unknown; headers: Record<string, string>; readChecked: boolean }
const inputClass = 'block w-full rounded border p-2';
const equalScope = (actual: Partial<KnowledgeSelection> | undefined, expected: KnowledgeSelection) =>
  actual && (Object.keys(expected) as (keyof KnowledgeSelection)[]).every(key => actual[key] === expected[key]);
const sameIdentity = (actual: Partial<KnowledgeSelection> | undefined, expected: KnowledgeSelection) =>
  actual && (['tenant_id', 'product_id', 'project_id', 'configuration_id', 'binding_id'] as const).every(key => actual[key] === expected[key]);
const alive = (value?: string) => Boolean(value && Number.isFinite(Date.parse(value)) && Date.parse(value) > Date.now());
const arrivalRecorded = (value: NativeArrival, scope: KnowledgeSelection) => value.service_id === 'devops'
  && value.subject === 'native_ui' && equalScope(value.snapshot?.selection, scope)
  && value.observation_receipt?.provenance === 'authenticated-human-observation'
  && value.observation_receipt.observed?.observation === 'displayed';

export default function DevOpsKnowledgeEvidence({ selection, disabled, nativeArrival, operationId, onAccessDenied }: Props) {
  const [operationInput, setOperationInput] = useState(operationId ?? '');
  const [operations, setOperations] = useState<KnowledgeOperation[]>([]);
  const [operationsLoaded, setOperationsLoaded] = useState(false);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [page, setPage] = useState<{ value: KnowledgeEvidencePage; etag: string } | null>(null);
  const [source, setSource] = useState('');
  const [arrival, setArrival] = useState<NativeArrival | null>(null);
  const [flow, setFlow] = useState('');
  const [challenge, setChallenge] = useState('');
  const [selected, setSelected] = useState<KnowledgeEvidence | null>(null);
  const [artifact, setArtifact] = useState<{ scenario: KnowledgePreparation; observation?: KnowledgeObservation } | null>(null);
  const [reviews, setReviews] = useState<Record<string, { evidence: string; explanation: string; confirmed: boolean }>>({});
  const [readEvidence, setReadEvidence] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState<SavedRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [, tick] = useState(0);
  const generation = useRef(0);
  const scopeKey = JSON.stringify(selection);
  useEffect(() => {
    generation.current += 1;
    setPage(null); setSource(''); setArrival(null); setSelected(null); setArtifact(null);
    setReviews({}); setReadEvidence({}); setPending(null); setBusy(false); setError(''); setMessage('');
    setOperations([]); setOperationsLoaded(false); setNextOffset(null);
    return () => { generation.current += 1; };
  }, [scopeKey, disabled]);
  useEffect(() => { const timer = window.setInterval(() => tick(value => value + 1), 1000); return () => window.clearInterval(timer); }, []);
  useEffect(() => { if (nativeArrival && arrivalRecorded(nativeArrival, selection)) setArrival(nativeArrival); }, [nativeArrival, selection]);

  function fail(failure: unknown) {
    const status = (failure as { status?: number })?.status;
    setError(knowledgeError(failure));
    if (status === 401 || status === 403 || status === 404) {
      setPage(null); setArtifact(null); setSelected(null); setReviews({}); setReadEvidence({}); setArrival(null); setPending(null);
      onAccessDenied();
    }
  }
  async function discover(offset = 0) {
    if (busy) return;
    const request = generation.current;
    setBusy(true); setError('');
    try {
      const result = await listKnowledgeOperations(selection.product_id, selection.project_id, selection.binding_id, offset);
      if (request !== generation.current) return;
      if (!Array.isArray(result.items) || result.items.some(item => item.binding_id !== selection.binding_id)) throw { status: 403 };
      setOperations(current => offset === 0 ? result.items : [...current, ...result.items.filter(item => !current.some(existing => existing.operation_id === item.operation_id))]);
      setOperationsLoaded(true); setNextOffset(result.next_offset);
    } catch (failure) { if (request === generation.current) fail(failure); }
    finally { if (request === generation.current) setBusy(false); }
  }
  async function refresh(operation: string, request = generation.current) {
    const loaded = await readKnowledgeEvidence(selection.product_id, selection.project_id, operation);
    if (request !== generation.current) return;
    if (loaded.value.operation_id !== operation || !sameIdentity(loaded.value.selection, selection)
      || !Array.isArray(loaded.value.publications) || !Array.isArray(loaded.value.items)
      || typeof loaded.value.eligibility?.can_prepare !== 'boolean' || typeof loaded.value.eligibility?.can_review !== 'boolean'
      || loaded.etag !== `"${loaded.value.revision}"`) throw { status: 409, detail: { next_action: 'This operation belongs to a different binding or Product. Select its original Product and binding before continuing.' } };
    setPage(loaded);
    if (loaded.value.eligibility.blocked_code) { setArtifact(null); setSelected(null); setReviews({}); setReadEvidence({}); }
    setSource(current => loaded.value.publications.some(item => item.source_key === current) ? current : '');
    setPending(current => current ? { ...current, readChecked: true } : null);
    return loaded;
  }
  async function load() {
    if (busy) return;
    const request = ++generation.current;
    setBusy(true); setError(''); setArtifact(null); setSelected(null); setPage(null);
    try { await refresh(pending && page ? page.value.operation_id : operationInput.trim(), request); }
    catch (failure) { if (request === generation.current) fail(failure); }
    finally { if (request === generation.current) setBusy(false); }
  }
  const path = page ? knowledgeEvidencePath(selection.product_id, selection.project_id, page.value.operation_id) : '';
  const publication = page?.value.publications.find(item => item.source_key === source);
  const currentSelection = Boolean(page && equalScope(page.value.selection, selection));
  const writable = Boolean(page && currentSelection && page.value.state === 'waiting' && !disabled && !busy && !pending);
  const sourcesCurrent = Boolean(page?.value.publications.length && page.value.publications.every(item => alive(item.source_expires_at)));
  const preparations = page?.value.items.filter(item => item.kind === 'prepared') ?? [];
  const observations = page?.value.items.filter(item => item.kind === 'observed') ?? [];
  const sourceName = (key: string) => {
    const item = page?.value.publications.find(value => value.source_key === key);
    return typeof item?.metadata.name === 'string' ? item.metadata.name : item?.native_type === 'skill' ? 'Selected skill' : `Instructions (${String(item?.metadata.agent_type ?? 'selected agent').toLowerCase().replace(/_/g, ' ')})`;
  };

  async function save(saved: SavedRequest) {
    if (!page || !currentSelection || disabled || busy) return;
    const request = generation.current;
    const operation = page.value.operation_id;
    setBusy(true); setError(''); setMessage('');
    try {
      await apiClient.request(saved.path, { method: 'POST', headers: saved.headers, body: saved.body });
      if (request !== generation.current) return;
      setPending(null);
      await refresh(operation, request);
      if (request === generation.current) setMessage('Evidence saved and read back. Review the retained state below before continuing.');
    } catch (failure) {
      if (request !== generation.current) return;
      const status = (failure as { status?: number })?.status;
      if (!status || status >= 500) {
        setPending({ ...saved, readChecked: false });
        try { await refresh(operation, request); } catch (readFailure) {
          if (request !== generation.current) return;
          setPage(null); setArtifact(null); setSelected(null);
          if ([401, 403, 404].includes((readFailure as { status?: number })?.status ?? 0)) { fail(readFailure); return; }
        }
      } else {
        setPending(null);
        if (status === 412 || status === 409) setPage(null);
      }
      if (request === generation.current) fail(failure);
    } finally { if (request === generation.current) setBusy(false); }
  }
  async function inspect(evidence: KnowledgeEvidence) {
    if (!page || busy) return;
    const request = generation.current;
    setBusy(true); setError(''); setSelected(null); setArtifact(null);
    const prepared = preparations.find(item => item.evidence_id === evidence.evidence_id);
    const observed = observations.find(item => item.evidence_id === evidence.evidence_id);
    try {
      if (!prepared) throw new Error('Missing preparation');
      const [scenario, observation] = await Promise.all([
        readKnowledgeArtifact<KnowledgePreparation>(path, evidence.evidence_id, 'prepared'),
        observed ? readKnowledgeArtifact<KnowledgeObservation>(path, evidence.evidence_id, 'observed') : Promise.resolve(undefined),
      ]);
      if (request !== generation.current) return;
      if (scenario.event_hash !== prepared.event_hash || scenario.evidence_id !== evidence.evidence_id
        || (observed && (observation?.event_hash !== observed.event_hash || observation.evidence_id !== evidence.evidence_id))) throw new Error('Evidence identity changed');
      setSelected(prepared); setArtifact({ scenario: scenario.artifact, observation: observation?.artifact });
      if (observed) setReadEvidence(current => ({ ...current, [observed.event_hash]: true }));
    } catch (failure) { if (request === generation.current) fail(failure); }
    finally { if (request === generation.current) setBusy(false); }
  }
  async function attachArrival() {
    const request = generation.current;
    setBusy(true); setError(''); setArrival(null);
    try {
      const value = await apiClient.get<NativeArrival>(`/orchestration/flows/${encodeURIComponent(flow.trim())}/l5-observations/${encodeURIComponent(challenge.trim())}`);
      if (request !== generation.current) return;
      if (!arrivalRecorded(value, selection) || !alive(value.expires_at)) throw { status: 409, detail: { next_action: 'Select your recorded native DevOps arrival for this binding and configuration. Its observation window must still be open.' } };
      setArrival({ ...value, flowId: flow.trim() });
    } catch (failure) { if (request === generation.current) fail(failure); }
    finally { if (request === generation.current) setBusy(false); }
  }
  const allReviewed = Boolean(page?.value.publications.length && page.value.publications.every(item => {
    const review = reviews[item.source_key];
    const observed = observations.find(record => record.evidence_id === review?.evidence && record.source_key === item.source_key);
    return review?.confirmed && review.explanation.trim().length >= 30 && observed && readEvidence[observed.event_hash];
  }));
  return <details className="space-y-4" onToggle={event => { if (event.target === event.currentTarget && event.currentTarget.open && !operationsLoaded) void discover(); }}>
    <summary>Review published knowledge use</summary>
    <p>Complete the human observation and review for an existing knowledge publication.</p>
    <label className="block">Knowledge publication operation<select className={inputClass} value={operationInput} disabled={busy || Boolean(pending)} onChange={event => { setOperationInput(event.target.value); setPage(null); setSelected(null); setArtifact(null); setReviews({}); setReadEvidence({}); }}><option value="">Choose an original publication</option>{operations.map(item => <option key={item.operation_id} value={item.operation_id}>Configuration {item.configuration_revision}, binding generation {item.binding_generation} — {item.state}</option>)}</select></label>
    <button type="button" disabled={busy} onClick={() => void discover()}>Refresh publication operations</button>
    {nextOffset !== null && <button type="button" disabled={busy} onClick={() => void discover(nextOffset)}>More publication operations</button>}
    {operationsLoaded && !operations.length && <p>No knowledge publication operation is retained for this binding.</p>}
    <button type="button" onClick={() => void load()} disabled={busy || !operationInput.trim()}>Read knowledge evidence</button>
    {disabled && <p>This configuration is disabled. Retained evidence can be read; evidence changes are unavailable.</p>}
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {pending && <p>A request is awaiting confirmation. {pending.readChecked ? 'Readback checked; retrying uses the same saved request.' : 'Read the original operation before retrying.'} <button type="button" disabled={busy || disabled || !currentSelection || !pending.readChecked || !page} onClick={() => void save(pending)}>Retry saved evidence request</button></p>}
    {page && <>
      {!currentSelection && <p>This is retained history from configuration {page.value.selection.configuration_revision}, binding generation {page.value.selection.binding_generation}. Evidence can be read; changes require the current configuration and generation.</p>}
      <p>Knowledge operation: {page.value.state}. {page.value.state === 'succeeded' ? 'Knowledge review is complete. Release acceptance remains separate.' : page.value.state !== 'waiting' ? 'This operation is read-only. Resolve its current delivery status before collecting evidence.' : 'Review each selected publication below.'}</p>
      {page.value.eligibility.blocked_code && <p>Evidence changes are unavailable. {knowledgeError({ detail: { code: page.value.eligibility.blocked_code, next_action: ['knowledge_evidence_source_access_revoked', 'knowledge_evidence_source_expired', 'knowledge_evidence_publication_not_ready', 'knowledge_evidence_expired', 'knowledge_evidence_inactive_requires_non_use'].includes(page.value.eligibility.blocked_code) ? undefined : 'Read the original operation and resolve its reported access or readiness issue before continuing.' } })}</p>}
      {!sourcesCurrent && <p>Retained source evidence has expired or is unavailable. Preserve this operation for review before further action.</p>}
      <label className="block">Published knowledge<select className={inputClass} value={source} disabled={busy || Boolean(pending)} onChange={event => setSource(event.target.value)}><option value="">Choose a publication</option>{page.value.publications.map(item => <option key={item.source_key} value={item.source_key}>{sourceName(item.source_key)} — {item.native_type === 'skill' ? 'skill' : 'instructions'}</option>)}</select></label>
      {publication && <>
        <p>{sourceName(source)}: native Asset version {publication.version ?? 'not confirmed'}.</p>
        {!publication.active && <p>This publication is inactive. Positive-use evidence is unavailable; keep it for the separate non-use review.</p>}
        <details><summary>Use an existing native arrival observation</summary>
          <p>The arrival recorded above is reused when it matches this selection. You can also attach its retained reference.</p>
          <label className="block">Knowledge observation delivery flow reference<input className={inputClass} value={flow} onChange={event => setFlow(event.target.value)} /></label>
          <label className="block">Recorded native arrival reference<input className={inputClass} value={challenge} onChange={event => setChallenge(event.target.value)} /></label>
          <button type="button" disabled={busy || !currentSelection || !flow.trim() || !challenge.trim()} onClick={() => void attachArrival()}>Read native arrival receipt</button>
        </details>
        <p>{currentSelection && arrival && alive(arrival.expires_at) ? `Native arrival verified. Observation window ends ${new Date(arrival.expires_at).toLocaleString()}.` : currentSelection ? 'Record or attach your current native DevOps arrival before preparing a scenario.' : 'A current native arrival cannot authorize changes to this historical operation.'}</p>
        <KnowledgePreparationForm key={`${page.value.operation_id}:${source}`} disabled={!writable || !page.value.eligibility.can_prepare || !sourcesCurrent || !publication.active || !arrival || !alive(arrival.expires_at)} onSubmit={value => {
          if (!writable || !page.value.eligibility.can_prepare || !arrival || !alive(arrival.expires_at)) return;
          void save({ path: `${path}/prepare`, body: { ...value, source_key: source, flow_id: arrival.flowId, challenge_id: arrival.challenge_id }, headers: { 'If-Match': page.etag, 'Idempotency-Key': productReceipt() }, readChecked: false });
        }} />
      </>}
      <h3>Saved observations</h3>
      {!preparations.length && <p>No scenario has been prepared for this operation.</p>}
      <ul>{preparations.map(item => <li key={item.evidence_id}>{sourceName(item.selection?.source_key ?? '')} — {observations.some(record => record.evidence_id === item.evidence_id) ? 'observation recorded' : alive(item.expires_at) ? 'awaiting your observation' : 'observation window expired'} <button type="button" disabled={busy} onClick={() => void inspect(item)}>Read scenario and evidence</button></li>)}</ul>
      {selected && artifact && <section aria-label="Selected knowledge evidence" className="space-y-3">
        <h3>Retained scenario</h3><p>Prompt: {artifact.scenario.prompt}</p><p>Expected behavior: {artifact.scenario.expected_behavior}</p><p>Source excerpt: {artifact.scenario.source_excerpt}</p>
        {artifact.observation ? <>
          <h4>Recorded conversation</h4>{artifact.observation.transcript.map((turn, index) => <p key={index}><strong>{turn.role === 'user' ? 'Human' : 'DevOps'}:</strong> {turn.text}</p>)}
          <p>Source effect: {artifact.observation.explanation}</p><p>Response quote: {artifact.observation.response_quote}</p><p>Source citation: {artifact.observation.source_citation_quote}</p>
          {artifact.observation.skill_invocation_quote && <p>Skill invocation: {artifact.observation.skill_invocation_quote}</p>}
          <details><summary>Actual before and after readbacks</summary><pre className="whitespace-pre-wrap break-words">{JSON.stringify({ before: artifact.observation.before, after: artifact.observation.after }, null, 2)}</pre></details>
        </> : observations.some(record => record.evidence_id === selected.evidence_id) ? <p>This observation is recorded. Read its saved evidence before reviewing it.</p> : <>
          {!alive(selected.expires_at) && <p>This collection window expired. Keep this preparation and prepare a fresh scenario before a new manually initiated session.</p>}
          <KnowledgeObservationForm key={selected.evidence_id} skill={selected.selection?.native_type === 'skill'} disabled={!writable || !page.value.eligibility.can_prepare || !sourcesCurrent || !alive(selected.expires_at)} onSubmit={value => {
            if (!writable || !page.value.eligibility.can_prepare || !alive(selected.expires_at)) return;
            void save({ path: `${path}/${encodeURIComponent(selected.evidence_id)}/observe`, body: { ...value, preparation_hash: selected.event_hash }, headers: {}, readChecked: false });
          }} />
        </>}
      </section>}
      <h3>Review and complete</h3>
      <p>Read one recorded observation for every publication. Explain why its actual response demonstrates use of the selected source.</p>
      {page.value.publications.map(item => {
        const review = reviews[item.source_key] ?? { evidence: '', explanation: '', confirmed: false };
        const records = observations.filter(record => record.source_key === item.source_key);
        const observed = records.find(record => record.evidence_id === review.evidence);
        return <fieldset key={item.source_key} disabled={!writable || !page.value.eligibility.can_review || !sourcesCurrent} className="space-y-2"><legend>{sourceName(item.source_key)}</legend>
          <label className="block">Observation to review for {sourceName(item.source_key)}<select className={inputClass} value={review.evidence} onChange={event => setReviews({ ...reviews, [item.source_key]: { evidence: event.target.value, explanation: '', confirmed: false } })}><option value="">Choose recorded evidence</option>{records.map(record => <option key={record.evidence_id} value={record.evidence_id}>{new Date(record.created_at).toLocaleString()}</option>)}</select></label>
          <button type="button" disabled={!observed} onClick={() => observed && void inspect(observed)}>Read this review evidence</button>
          <label className="block">Your review of {sourceName(item.source_key)}<textarea className={inputClass} minLength={30} maxLength={4000} value={review.explanation} onChange={event => setReviews({ ...reviews, [item.source_key]: { ...review, explanation: event.target.value } })} /></label>
          <label><input type="checkbox" disabled={!observed || !readEvidence[observed.event_hash]} checked={review.confirmed} onChange={event => setReviews({ ...reviews, [item.source_key]: { ...review, confirmed: event.target.checked } })} /> I reviewed the actual response and its source-dependent behavior.</label>
        </fieldset>;
      })}
      <button type="button" disabled={!writable || !page.value.eligibility.can_review || !sourcesCurrent || !allReviewed} onClick={() => {
        if (!writable || !page.value.eligibility.can_review || !allReviewed) return;
        void save({ path: `${path}/complete`, body: { reviews: page.value.publications.map(item => {
          const review = reviews[item.source_key];
          return { evidence_id: review.evidence, observation_hash: observations.find(record => record.evidence_id === review.evidence)!.event_hash, source_effect_explanation: review.explanation };
        }) }, headers: { 'If-Match': page.etag, 'Idempotency-Key': productReceipt() }, readChecked: false });
      }}>Complete knowledge review</button>
      <DevOpsKnowledgeAbandon key={page.value.operation_id} selection={selection} operationId={page.value.operation_id} onAccessDenied={onAccessDenied} onChanged={() => void load()} />
    </>}
  </details>;
}
