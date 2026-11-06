import { useCallback, useEffect, useRef, useState } from 'react';
import { compareSecurityResults, getSecurityPublication, getSecurityResult, listSecurityFindings, listSecurityResults, listSecurityTriage, retrySecurityPublication, setSecurityTriage, matchesSecurityScope, type SecurityComparison, type SecurityConfiguration, type SecurityFinding, type SecurityPublication, type SecurityResult, type SecurityTriage, type SecurityTriageInput, type Versioned } from '@/services/l5Services';
import { productReceipt, type RecordReference } from '@/services/products';
import { SecurityBadge, securityDate, securityResultName, securitySeverityRank, securityLocationLabel } from './SecurityPresentation';
import { ServiceDisclosure } from './L5ServiceJourney';

interface Props { productId: string; projectId: string; configuration: Versioned<SecurityConfiguration> | null; onAccessDenied?: () => void }
const denied = (error: unknown) => typeof error === 'object' && error !== null && 'status' in error && (error.status === 403 || error.status === 404);
const label = (source: RecordReference) => `${source.record_type} ${source.record_id} (revision ${source.record_revision})`;

export default function SecurityResults({ productId, projectId, configuration, onAccessDenied }: Props) {
  const [results, setResults] = useState<SecurityResult[]>([]);
  const [resultCursor, setResultCursor] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [result, setResult] = useState<SecurityResult | null>(null);
  const [findings, setFindings] = useState<SecurityFinding[]>([]);
  const [findingCursor, setFindingCursor] = useState<string | null>(null);
  const [findingId, setFindingId] = useState('');
  const [triage, setTriage] = useState<(Versioned<{ items: SecurityTriage[] }> & { resultId: string; findingId: string }) | null>(null);
  const [decision, setDecision] = useState<SecurityTriageInput['decision']>('open');
  const [triageReason, setTriageReason] = useState('');
  const [baselineId, setBaselineId] = useState('');
  const [comparison, setComparison] = useState<SecurityComparison | null>(null);
  const [publication, setPublication] = useState<SecurityPublication | null>(null);
  const [publicationReason, setPublicationReason] = useState('');
  const [pending, setPending] = useState<{ reason: string; receipt: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadingResults, setLoadingResults] = useState(false);
  const [loadingFindings, setLoadingFindings] = useState(false);
  const [error, setError] = useState('');
  const revoked = useRef(false);
  const listGeneration = useRef(0);
  const resultGeneration = useRef(0);
  const triageGeneration = useRef(0);
  const comparisonGeneration = useRef(0);

  const clearDetail = useCallback(() => {
    resultGeneration.current++; triageGeneration.current++; comparisonGeneration.current++;
    setResult(null); setFindings([]); setFindingCursor(null); setFindingId(''); setTriage(null); setLoadingFindings(false);
    setComparison(null); setPublication(null); setPending(null); setBaselineId('');
  }, []);
  const revoke = useCallback(() => {
    revoked.current = true; listGeneration.current++; clearDetail(); setResults([]); setResultCursor(null); setSelectedId(''); setLoadingResults(false); onAccessDenied?.();
  }, [clearDetail, onAccessDenied]);
  const matchesScope = (item: SecurityResult) => matchesSecurityScope(item.scope, configuration?.value.scope, productId, projectId);
  const matchesFindings = (items: SecurityFinding[]) => items.every(item => item.source_refs.every(source =>
    source.tenant_id === configuration?.value.scope.tenant_id && source.project_id === projectId));
  const refresh = useCallback(async () => {
    if (revoked.current) return;
    const generation = ++listGeneration.current;
    setLoadingResults(false);
    clearDetail(); setSelectedId(''); setResults([]); setResultCursor(null);
    if (!configuration) return;
    try {
      const page = await listSecurityResults(productId, projectId);
      if (revoked.current || generation !== listGeneration.current) return;
      if (page.items.some(item => !matchesSecurityScope(item.scope, configuration.value.scope, productId, projectId))) throw { status: 403 };
      setResults(page.items); setResultCursor(page.next_cursor ?? null); setError('');
    } catch (readError) {
      if (revoked.current || generation !== listGeneration.current) return;
      if (denied(readError)) revoke();
      setError('Security results are unavailable for this workspace. No finding count or coverage is confirmed.');
    }
  }, [productId, projectId, configuration, clearDetail, revoke]);
  useEffect(() => {
    void refresh();
    return () => { listGeneration.current += 1; resultGeneration.current += 1; triageGeneration.current += 1; comparisonGeneration.current += 1; };
  }, [refresh]);

  async function inspect(id: string) {
    clearDetail(); setSelectedId(id);
    const generation = resultGeneration.current;
    if (!id) return;
    try {
      const current = await getSecurityResult(productId, projectId, id);
      if (revoked.current || generation !== resultGeneration.current) return;
      if (current.result_id !== id || !matchesScope(current)) throw { status: 403 };
      const page = await listSecurityFindings(productId, projectId, id);
      if (revoked.current || generation !== resultGeneration.current) return;
      if (!matchesFindings(page.items)) throw { status: 403 };
      setResult(current); setFindings(page.items); setFindingCursor(page.next_cursor ?? null); setError('');
      try {
        const nextPublication = current.publication_id ? await getSecurityPublication(productId, projectId, current.publication_id) : null;
        if (revoked.current || generation !== resultGeneration.current) return;
        if (nextPublication && nextPublication.result_id !== current.result_id) throw { status: 403 };
        setPublication(nextPublication);
      } catch (readError) {
        if (revoked.current || generation !== resultGeneration.current) return;
        if (denied(readError)) throw readError;
        setError('Publication status could not be read. The retained result and findings remain available. Refresh Security results and select this result again to retry publication readback.');
      }
    } catch (readError) {
      if (revoked.current || generation !== resultGeneration.current) return;
      clearDetail(); setSelectedId(''); if (denied(readError)) revoke();
      setError('Result, findings or publication are unavailable. Refresh before inspecting coverage or counts.');
    }
  }

  async function loadMoreResults() {
    if (!resultCursor || loadingResults || revoked.current) return;
    const generation = listGeneration.current;
    setLoadingResults(true);
    try {
      const page = await listSecurityResults(productId, projectId, resultCursor);
      if (revoked.current || generation !== listGeneration.current) return;
      if (page.items.some(item => !matchesScope(item))) throw { status: 403 };
      setResults(current => [...current.filter(item => !page.items.some(next => next.result_id === item.result_id)), ...page.items]);
      setResultCursor(page.next_cursor ?? null);
    } catch (readError) {
      if (revoked.current || generation !== listGeneration.current) return;
      if (denied(readError)) revoke();
      setError('More results could not be read. Retry loading this page or refresh Security results.');
    } finally { if (generation === listGeneration.current) setLoadingResults(false); }
  }

  async function loadMoreFindings() {
    if (!result || !findingCursor || loadingFindings || revoked.current) return;
    const generation = resultGeneration.current;
    setLoadingFindings(true);
    try {
      const page = await listSecurityFindings(productId, projectId, result.result_id, findingCursor);
      if (revoked.current || generation !== resultGeneration.current) return;
      if (!matchesFindings(page.items)) throw { status: 403 };
      setFindings(current => [...current.filter(item => !page.items.some(next => next.finding_id === item.finding_id)), ...page.items]);
      setFindingCursor(page.next_cursor ?? null);
    } catch (readError) {
      if (revoked.current || generation !== resultGeneration.current) return;
      if (denied(readError)) revoke();
      setError('More findings could not be read. Retry loading this page or refresh Security results.');
    } finally { if (generation === resultGeneration.current) setLoadingFindings(false); }
  }

  async function inspectFinding(id: string) {
    const generation = ++triageGeneration.current;
    setFindingId(id); setTriage(null); setTriageReason('');
    if (!result || result.result_id !== selectedId || !id) return;
    try {
      const current = await listSecurityTriage(productId, projectId, result.result_id, id);
      if (revoked.current || generation !== triageGeneration.current) return;
      if (current.value.items.some(item => item.finding_id !== id)) throw { status: 403 };
      const latest = current.value.items.reduce<SecurityTriage | null>((head, item) => !head || item.revision > head.revision ? item : head, null);
      setDecision(latest?.decision.decision ?? 'open');
      setTriage({ ...current, resultId: result.result_id, findingId: id }); setError('');
    }
    catch (readError) {
      if (revoked.current || generation !== triageGeneration.current) return;
      if (denied(readError)) revoke();
      setError('Triage revision is unavailable. Refresh before editing this finding.');
    }
  }

  async function saveTriage() {
    if (!result || !findingId || !triage?.etag || !triageReason.trim() || busy) return;
    if (triage.resultId !== result.result_id || triage.resultId !== selectedId || triage.findingId !== findingId) return;
    const generation = triageGeneration.current;
    setBusy(true);
    try {
      const saved = await setSecurityTriage(productId, projectId, result.result_id, findingId, { decision, reason: triageReason.trim() }, triage.etag);
      if (revoked.current || generation !== triageGeneration.current) return;
      if (saved.value.finding_id !== findingId) throw { status: 403 };
      setTriage({ value: { items: [saved.value] }, etag: saved.etag, resultId: result.result_id, findingId }); setTriageReason(''); setError('');
    } catch (writeError) {
      if (revoked.current || generation !== triageGeneration.current) return;
      setTriage(null); if (denied(writeError)) revoke();
      setError(denied(writeError) ? 'Finding access was removed. Refresh Security results before continuing.'
        : typeof writeError === 'object' && writeError !== null && 'status' in writeError && writeError.status === 412
          ? 'Triage changed elsewhere (412). Refresh its revision and make a new decision before saving.'
          : 'Triage was not confirmed. Refresh its revision and review the decision before another edit.');
    } finally { setBusy(false); }
  }

  async function compare() {
    if (!result || !baselineId || baselineId === result.result_id || busy) return;
    const generation = ++comparisonGeneration.current;
    setBusy(true); setComparison(null);
    try {
      const read = await compareSecurityResults(productId, projectId, baselineId, result.result_id);
      if (revoked.current || generation !== comparisonGeneration.current) return;
      setComparison(read); setError('');
    }
    catch (readError) {
      if (revoked.current || generation !== comparisonGeneration.current) return;
      if (denied(readError)) revoke();
      setError('Comparison is unavailable; no resolved findings are confirmed.');
    }
    finally { setBusy(false); }
  }

  async function retry(reason: string, receipt: string) {
    if (!publication || busy) return;
    const generation = resultGeneration.current;
    setBusy(true);
    try {
      const updated = await retrySecurityPublication(productId, projectId, publication.publication_id, reason, receipt);
      if (revoked.current || generation !== resultGeneration.current) return;
      if (updated.result_id !== result?.result_id) throw { status: 403 };
      setPublication(updated);
      setPending(null); setPublicationReason(''); setError('');
    } catch (writeError) {
      if (revoked.current || generation !== resultGeneration.current) return;
      if (denied(writeError)) revoke(); else setPending({ reason, receipt });
      setError('Publication retry was not confirmed. Keep the original receipt; the assessment is not restarted.');
    } finally { setBusy(false); }
  }

  const finding = findings.find(item => item.finding_id === findingId);
  const latestTriage = triage?.value.items.reduce<SecurityTriage | null>((latest, item) => !latest || item.revision > latest.revision ? item : latest, null);
  return <section id="security-results" aria-label="Security results" className="security-workspace security-panel">
    <div className="security-panel-heading"><div><h3>Assessment results</h3><p>Review findings, understand the evidence, and record your decision.</p></div>
      {configuration && <button type="button" onClick={() => void refresh()} disabled={busy}>Refresh Security results</button>}
    </div>
    {error && <p role="alert">{error}</p>}
    {!configuration ? <p>Security configuration is unavailable for this workspace.</p> : <>
      <label className="block">Visible results <select value={selectedId} onChange={event => void inspect(event.target.value)}>
        <option value="">Select a result</option>{results.map(item => <option key={item.result_id} value={item.result_id}>{securityResultName(item)} · {securityDate(item.observed_at)} · {item.finding_count ?? "Unknown"} findings · {item.result_id.length > 16 ? item.result_id.slice(0, 8) : item.result_id}</option>)}
      </select></label>
      {!results.length && <p className="security-empty">No Security results are visible. This does not establish a completed or passed scan.</p>}
      {resultCursor && <button type="button" disabled={busy || loadingResults} onClick={() => void loadMoreResults()}>Load more results</button>}
      {result && <div aria-label="Security result detail" className="space-y-4">
        <div className="security-panel-heading"><div><h3>{securityResultName(result)}</h3><p>{securityDate(result.observed_at)}</p></div>{result.provider_outcome && <SecurityBadge value={result.provider_outcome} />}</div>
        <div className="security-metrics" aria-label="Result summary">
          <div className="security-metric"><strong>{result.finding_count ?? '—'}</strong><span>Reported findings</span></div>
          <div className="security-metric"><SecurityBadge value={result.coverage} /><span>Source coverage</span></div>
          {result.publication_id && <div className="security-metric"><SecurityBadge value={publication?.state ?? 'unavailable'} /><span>Result delivery</span></div>}
        </div>
        <p className="security-note">{result.coverage === 'complete' ? 'Assessment coverage is complete. Findings are provider claims that need review; this is not a guarantee of security.' : 'Coverage is incomplete or unknown. Review the findings, but do not treat unchecked sources as clean.'}</p>
        <ServiceDisclosure title="Assessment evidence and references" hint="Source revisions, run identifiers and publication receipt">
        <div className="security-technical">
        <p>Result {result.result_id}, revision {result.revision}; run {result.run_id}; native job {'record_type' in result.native_job_ref ? label(result.native_job_ref) : result.native_job_ref.threat_model_job_id}</p>
        <p>Observed {result.observed_at}; coverage {result.coverage} ({result.coverage_mode}); provider outcome {result.provider_outcome ?? 'unknown'}.</p>
        <p>Reported finding count: {result.finding_count ?? 'unavailable'}. {result.coverage === 'complete' ? 'Completed coverage is not proof that all threats were found.' : 'Coverage is incomplete or unknown; unchecked sources cannot be treated as clean.'}</p>
        <p>Adapter revision: {result.admission?.adapter_revision ?? 'unavailable'}; binding generation: {result.admission?.binding_generation ?? 'unavailable'}.</p>
        <p>Source snapshots: {result.admission?.source_snapshots.map(snapshot => `${label(snapshot.source_ref)} — ${snapshot.snapshot_id}`).join('; ') || 'Unavailable'}</p>
        <p>Publication state: {publication?.state ?? (result.publication_id ? 'unavailable' : 'not requested')}. {publication?.state === 'published' && publication.receipt_id && publication.consumer_revision
          ? `Receipt ${publication.receipt_id}; consumer revision ${publication.consumer_revision}.` : 'Result publication is not confirmed.'}</p>
        </div></ServiceDisclosure>
        {publication && ['retry_wait', 'unavailable'].includes(publication.state) && <div className="security-note">
          <h4>Result publication needs attention</h4><p>The findings are retained. Retry delivery to the Product without restarting the assessment.</p>
          <label className="block">Publication retry reason <input value={publicationReason} onChange={event => setPublicationReason(event.target.value)} /></label>
          <button type="button" disabled={busy || (!pending && !publicationReason.trim())} onClick={() => {
            const attempt = pending ?? { reason: publicationReason.trim(), receipt: productReceipt() };
            void retry(attempt.reason, attempt.receipt);
          }}>{pending ? 'Retry original publication receipt' : 'Retry publication only'}</button>
        </div>}
        <ServiceDisclosure title="Compare with a previous result" hint="Check new, unchanged and resolved findings">
        <label className="block">Baseline result <select value={baselineId} onChange={event => { comparisonGeneration.current++; setBaselineId(event.target.value); setComparison(null); }}>
          <option value="">Select a previous result</option>{results.filter(item => item.result_id !== result.result_id).map(item =>
            <option key={item.result_id} value={item.result_id}>{securityResultName(item)} · {securityDate(item.observed_at)} · {item.result_id.slice(0, 8)}</option>)}
        </select></label>
        <button type="button" disabled={!baselineId || busy} onClick={() => void compare()}>Compare results</button>
        {comparison && <div aria-label="Result comparison">
          <p>Comparability: {comparison.comparability}. {comparison.reasons.join('; ') || 'No reasons returned.'}</p>
          <p>Baseline revision {comparison.baseline_revision ?? 'unknown'}; candidate revision {comparison.candidate_revision ?? 'unknown'}.</p>
          <p>New finding IDs: {comparison.new_findings?.join(', ') || 'None reported'}; unchanged finding IDs: {comparison.unchanged_findings?.join(', ') || 'None reported'}.</p>
          <p>{comparison.comparability === 'comparable' && result.coverage === 'complete'
            ? `Reported resolved finding IDs: ${comparison.resolved_findings?.join(', ') || 'None reported'}. Validate independently.`
            : 'Resolved findings cannot be inferred from partial or incomparable coverage.'}</p>
        </div>}
        </ServiceDisclosure>
        <div className="security-panel-heading"><div><h3>Findings to review</h3><p>{findings.length} loaded{findingCursor ? ' · more findings available' : ''}. Candidate threats need independent validation.</p></div></div>
        <div className="security-list security-finding-list" aria-label="Finding list">
          {[...findings].sort((a, b) => securitySeverityRank(a.provider_severity) - securitySeverityRank(b.provider_severity)).map(item => <button type="button" className="security-list-row" key={item.finding_id} aria-pressed={findingId === item.finding_id} onClick={() => void inspectFinding(item.finding_id)}>
            <span><strong>{item.claim}</strong><small>{item.kind === 'candidate_threat' ? 'Candidate threat' : 'Static finding'} · {item.locations[0] ? securityLocationLabel(item.locations[0]) : 'See source evidence'}</small></span><SecurityBadge value={item.provider_severity} />
          </button>)}
        </div>
        <ServiceDisclosure title="Select finding by reference">
        <label className="block">Visible findings <select value={findingId} onChange={event => void inspectFinding(event.target.value)}>
          <option value="">Select a finding</option>{findings.map(item => <option key={item.finding_id} value={item.finding_id}>{item.finding_id} — {item.provider_severity}</option>)}
        </select></label>
        </ServiceDisclosure>
        {!findings.length && <p className="security-empty">No findings returned in this page. Zero findings does not prove a passed scan.</p>}
        {findingCursor && <button type="button" disabled={busy || loadingFindings} onClick={() => void loadMoreFindings()}>Load more findings</button>}
        {finding && <div aria-label="Finding detail" className="security-panel">
          <div className="security-panel-heading"><h3>{finding.kind === 'candidate_threat' ? 'Candidate threat' : 'Static finding'}</h3><SecurityBadge value={finding.provider_severity} /></div>
          <p className="security-claim">{finding.claim}</p>
          <div className="security-mitigations"><h4>Recommended mitigation</h4>{finding.mitigations.length ? <ul>{finding.mitigations.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p>No mitigation was supplied. Review the evidence before deciding the next action.</p>}</div>
          {finding.assumptions.length > 0 && <div><h4>Assumptions to verify</h4><ul>{finding.assumptions.map((item, index) => <li key={index}>{item}</li>)}</ul></div>}
          <p>Provider severity: {finding.provider_severity}; validation: {finding.validation}; human triage: {latestTriage?.decision.decision ?? 'not recorded'}.</p>
          <ServiceDisclosure title="Finding evidence" hint="Locations, source references and provider identifiers"><div className="security-technical">
          <p>{finding.kind === 'candidate_threat' ? 'Candidate threat' : 'Static finding'} {finding.finding_id} (native {finding.native_id}): {finding.claim}</p>
          <p>Source revisions: {finding.source_refs.map(label).join('; ') || 'Unavailable'}</p>
          <p>Locations: {finding.locations.map(securityLocationLabel).join('; ') || 'Unavailable'}; evidence: {finding.evidence.map(item => `${item.artifact_id} (revision ${item.version})`).join('; ') || 'Unavailable'}</p>
          <p>Assumptions: {finding.assumptions.join('; ') || 'None recorded'}; mitigations: {finding.mitigations.join('; ') || 'None recorded'}</p>
          </div></ServiceDisclosure>
          <h4>Review decision</h4>
          <button type="button" disabled={busy} onClick={() => void inspectFinding(finding.finding_id)}>Refresh finding triage</button>
          {triage && <div>
            <label className="block">Triage decision <select value={decision} onChange={event => setDecision(event.target.value as SecurityTriageInput['decision'])}>
              {(['open', 'accepted_risk', 'false_positive', 'resolved'] as const).map(item => <option key={item} value={item}>{item.replace(/_/g, ' ')}</option>)}
            </select></label>
            <label className="block">Triage reason <textarea value={triageReason} onChange={event => setTriageReason(event.target.value)} /></label>
            <button type="button" disabled={busy || !triageReason.trim()} onClick={() => void saveTriage()}>Save triage decision</button>
          </div>}
        </div>}
      </div>}
    </>}
  </section>;
}
