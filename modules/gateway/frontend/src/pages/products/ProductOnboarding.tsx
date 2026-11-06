import { useEffect, useRef, useState } from 'react';
import { useFeaturesQuery } from '@/hooks/useFeatures';
import { confirmFinancialDate, getOnboarding, getProduct, guideOnboarding, listInitiatives, listProductContributions, listProductChanges, listSourceBindings, productReceipt, retryOnboardingDiscovery, unlinkOnboardingSource, saveFinancialsStep, setFinancialsApplicabilityStep, startOnboarding, type FinancialAllocation, type InitiativeRecord, type ProductContribution, type RecordReference, type OnboardingGuidance, type OnboardingSession, type ProductProfile, type SourceBinding } from '@/services/products';

type Props = { productId: string; projectId: string; revision: number; userId: string; orgId: string; onSaved?: () => void };

const blankAllocation = () => ({ name: '', amount: '', currency: 'USD', category: 'ai' as FinancialAllocation['category'], period_start: '', period_end: '' });

const referenceValue = (reference: RecordReference) => JSON.stringify({
  tenant_id: reference.tenant_id, project_id: reference.project_id, source_connection_id: reference.source_connection_id,
  record_type: reference.record_type, record_id: reference.record_id, record_revision: reference.record_revision,
});

export default function ProductOnboarding({ productId, projectId, revision, userId, orgId, onSaved }: Props) {
  const storageKey = `product-onboarding:${orgId}:${userId}:${productId}:${projectId}`;
  const receiptKey = `${storageKey}:receipt`;
  const discoveryReceiptKey = `${storageKey}:discovery-receipt`;
  const unlinkReceiptKey = `${storageKey}:unlink-receipt`;
  const scopeRef = useRef(storageKey);
  scopeRef.current = storageKey;
  const { data: features, isError: featuresError } = useFeaturesQuery();
  const connectorsEnabled = !featuresError && features?.devlake_connectors === true;
  const [loadedKey, setLoadedKey] = useState('');
  const [session, setSession] = useState<OnboardingSession | null>(null);
  const [bindings, setBindings] = useState<SourceBinding[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [guidanceInput, setGuidanceInput] = useState('');
  const [meteredGuidance, setMeteredGuidance] = useState(false);
  const [guidanceProposals, setGuidanceProposals] = useState<OnboardingGuidance['proposals']>([]);
  const [guidanceMessage, setGuidanceMessage] = useState('');
  const documentRequest = useRef<{ receipt: string; revision: number } | null>(null);
  const [profile, setProfile] = useState<ProductProfile | null>(null);
  const [targetRevision, setTargetRevision] = useState('');
  const [initiative, setInitiative] = useState('');
  const [catalogInitiatives, setCatalogInitiatives] = useState<InitiativeRecord[]>([]);
  const [contributions, setContributions] = useState<ProductContribution[]>([]);
  const [catalogCoverage, setCatalogCoverage] = useState<'loading' | 'ready' | 'unavailable' | 'restricted' | 'conflict'>('loading');
  const [catalogRefresh, setCatalogRefresh] = useState(0);
  const [releaseDate, setReleaseDate] = useState('');
  const [applicabilityReason, setApplicabilityReason] = useState('');
  const [allocation, setAllocation] = useState(blankAllocation);

  useEffect(() => {
    const sessionId = sessionStorage.getItem(storageKey);
    let active = true;
    setSession(null);
    setBindings([]);
    documentRequest.current = null;
    setGuidanceMessage('');
    setGuidanceProposals([]);
    setMeteredGuidance(false);
    setProfile(null);
    setTargetRevision('');
    setInitiative('');
    setCatalogInitiatives([]);
    setContributions([]);
    setCatalogCoverage('loading');
    setReleaseDate('');
    setApplicabilityReason('');
    setAllocation(blankAllocation());
    setLoadedKey('');
    if (sessionId) {
      void getOnboarding(productId, projectId, sessionId).then((saved) => {
        if (active) { setSession(saved); setGuidanceProposals(saved.proposals ?? []); setLoadedKey(storageKey); }
        return listSourceBindings(productId, projectId).then((sources) => {
          if (active) setBindings(sources.items);
        }).catch(() => { if (active) setError('Source bindings unavailable. Profile editing remains available.'); });
      }).catch(() => {
        if (active) { sessionStorage.removeItem(storageKey); setLoadedKey(storageKey); }
      });
    } else {
      setLoadedKey(storageKey);
    }
    return () => { active = false; };
  }, [storageKey, productId, projectId]);

  useEffect(() => {
    if (!session || loadedKey !== storageKey) return;
    let active = true;
    void Promise.all([getProduct(productId, projectId), listProductChanges(productId, projectId)]).then(([current, changes]) => {
      if (!active) return;
      setProfile(current);
      setTargetRevision(changes.items[0]?.event_id ?? '');
      setInitiative(current.configuration?.financials?.initiatives?.[0] ? referenceValue(current.configuration.financials.initiatives[0]) : '');
      setReleaseDate(current.configuration?.financials?.release_target_date ?? '');
      const savedAllocation = current.configuration?.financials?.allocations?.[0];
      if (savedAllocation) setAllocation({ name: savedAllocation.name, amount: savedAllocation.amount.amount,
        currency: savedAllocation.amount.currency, category: savedAllocation.category,
        period_start: savedAllocation.period_start, period_end: savedAllocation.period_end });
      else setAllocation(blankAllocation());
    }).catch(() => { if (active) setError('Could not read Financials. Retry after checking your connection.'); });
    return () => { active = false; };
  }, [session?.id, loadedKey, storageKey, productId, projectId]);

  useEffect(() => {
    if (!session || loadedKey !== storageKey || !profile) return;
    let active = true;
    setCatalogCoverage('loading');
    async function loadCatalog() {
      const records: InitiativeRecord[] = [];
      const visible: ProductContribution[] = [];
      let cursor: string | undefined;
      do {
        const page = await listInitiatives(cursor);
        records.push(...page.items);
        cursor = page.next_cursor;
      } while (cursor);
      cursor = undefined;
      do {
        const page = await listProductContributions(projectId, cursor);
        visible.push(...page.items.filter((item) => item.project_id === projectId
          && item.data.product_ref.record_id === productId && item.data.product_ref.project_id === projectId));
        cursor = page.next_cursor;
      } while (cursor);
      if (!active) return;
      setCatalogInitiatives(records);
      setContributions(visible);
      setCatalogCoverage('ready');
    }
    void loadCatalog().catch((failure) => {
      if (!active) return;
      setCatalogInitiatives([]);
      setContributions([]);
      const status = (failure as { status?: number }).status;
      setCatalogCoverage(status === 403 || status === 404 ? 'restricted'
        : status === 400 || status === 409 || status === 412 ? 'conflict' : 'unavailable');
    });
    return () => { active = false; };
  }, [session, profile, loadedKey, storageKey, projectId, productId, catalogRefresh]);

  async function saveFinancials(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !profile || !targetRevision || loading || (initiative && !initiativeOptions.some((option) => option.value === initiative))) return;
    setLoading(true);
    setError('');
    const previous = profile.configuration?.financials ?? {};
    const entry: FinancialAllocation = {
      id: previous.allocations?.[0]?.id ?? productReceipt(), name: allocation.name,
      target_ref: { tenant_id: profile.tenant_id, project_id: projectId, source_connection_id: 'adp.profile',
        record_type: 'product', record_id: productId, record_revision: targetRevision },
      amount: { amount: allocation.amount, currency: allocation.currency }, category: allocation.category,
      period_start: allocation.period_start, period_end: allocation.period_end, approval_status: 'proposed',
      owner_ref: { kind: 'user', id: profile.project?.owner_user_id ?? profile.product.owner_ref.id },
    };
    const selectedInitiative = initiative ? JSON.parse(initiative) as RecordReference : undefined;
    try {
      const updated = await saveFinancialsStep(productId, projectId, session.id, session.revision,
        { ...previous, allocations: [entry, ...(previous.allocations?.slice(1) ?? [])],
          initiatives: selectedInitiative ? [selectedInitiative] : [],
          release_target_date: releaseDate || undefined });
      const confirmed = await getProduct(productId, projectId);
      if (scopeRef.current !== storageKey) return;
      setProfile(confirmed);
      setSession(updated);
      setGuidanceProposals(updated.proposals ?? []);
      onSaved?.();
    } catch (failure) {
      if (scopeRef.current !== storageKey) return;
      const status = (failure as { status?: number }).status;
      if (status === 403 || status === 404 || status === 409 || status === 412) {
        setCatalogInitiatives([]);
        setContributions([]);
        setCatalogCoverage(status === 403 || status === 404 ? 'restricted' : 'conflict');
        setError(status === 403 || status === 404
          ? 'The selected Initiative is no longer accessible. Financials were not saved.'
          : 'Financials or the selected Initiative changed elsewhere. Your edits were not saved.');
      } else setError('Financials could not be confirmed. Read the latest Profile before retrying.');
    } finally {
      if (scopeRef.current === storageKey) setLoading(false);
    }
  }

  async function chooseApplicability(action: 'later' | 'not_applicable') {
    if (!session || loading || (action === 'not_applicable' && !applicabilityReason.trim())) return;
    setLoading(true);
    setError('');
    try {
      const updated = await setFinancialsApplicabilityStep(productId, projectId, session.id, session.revision, action,
        applicabilityReason.trim() || 'Deferred for later.');
      const confirmed = await getProduct(productId, projectId);
      if (scopeRef.current !== storageKey) return;
      setProfile(confirmed);
      setSession(updated);
      onSaved?.();
    } catch {
      if (scopeRef.current !== storageKey) return;
      setError('Could not save applicability. Check your access and mandatory checks, then retry.');
    } finally {
      if (scopeRef.current === storageKey) setLoading(false);
    }
  }

  async function askGuidance(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || loading || !guidanceInput.trim()) return;
    setLoading(true);
    setError('');
    setGuidanceMessage('');
    setGuidanceProposals([]);
    try {
      const result = await guideOnboarding(productId, projectId, session.id, session.revision, guidanceInput.trim(),
        meteredGuidance ? 'model' : 'deterministic');
      if (scopeRef.current === storageKey) {
        setGuidanceMessage(meteredGuidance && result.mode !== 'model' ? `Model unavailable. ${result.message}` : result.message);
        setGuidanceProposals(result.proposals);
      }
    } catch {
      if (scopeRef.current === storageKey) setError('Guidance unavailable. Direct edits remain available.');
    } finally {
      if (scopeRef.current === storageKey) setLoading(false);
    }
  }

  async function importDocuments() {
    if (!session || loading) return;
    setLoading(true); setError('');
    const request = documentRequest.current ?? { receipt: productReceipt(), revision: session.revision };
    documentRequest.current = request;
    try {
      const result = await guideOnboarding(productId, projectId, session.id, request.revision,
        'Read connected financial documents', 'financial', request.receipt);
      if (scopeRef.current !== storageKey) return;
      documentRequest.current = null;
      setGuidanceMessage(result.message);
      setGuidanceProposals(result.proposals);
    } catch (failure) {
      if (scopeRef.current !== storageKey) return;
      const status = (failure as { status?: number }).status;
      if (status && status >= 400 && status < 500) documentRequest.current = null;
      setError(status === 404 ? 'Connect an accessible financial document in Connections & people first.'
        : status === 422 ? 'The connected document needs a valid financial plan. Check its amount, currency and dates.'
        : status === 412 ? 'The profile changed. Resume onboarding to read the latest values before importing.'
        : 'Could not confirm document suggestions. Retry to check the same request. Saved values are unchanged.');
    } finally { if (scopeRef.current === storageKey) setLoading(false); }
  }

  async function resume() {
    if (loading) return;
    setLoading(true);
    setError('');
    const receipt = sessionStorage.getItem(receiptKey) ?? productReceipt();
    sessionStorage.setItem(receiptKey, receipt);
    try {
      const started = await startOnboarding(productId, projectId, revision, receipt);
      const saved = await getOnboarding(productId, projectId, started.id);
      if (scopeRef.current !== storageKey) return;
      sessionStorage.setItem(storageKey, saved.id);
      sessionStorage.removeItem(receiptKey);
      setSession(saved);
      void listSourceBindings(productId, projectId).then((sources) => {
        if (scopeRef.current === storageKey) setBindings(sources.items);
      }).catch(() => { if (scopeRef.current === storageKey) setError('Source bindings unavailable. Profile editing remains available.'); });
      setGuidanceProposals(saved.proposals ?? []);
      setLoadedKey(storageKey);
    } catch {
      if (scopeRef.current !== storageKey) return;
      setError('Could not confirm the session. Check your connection, then retry with the same request.');
    } finally {
      if (scopeRef.current === storageKey) setLoading(false);
    }
  }

  async function retryDiscovery(operationId: string) {
    if (!session || loading) return;
    const currentScope = storageKey;
    setLoading(true);
    setError('');
    try {
      const current = await getOnboarding(productId, projectId, session.id);
      if (scopeRef.current !== currentScope) return;
      setSession(current);
      const attempt = current.discoveries?.find((item) => item.id === operationId);
      if (!attempt?.cursor || !attempt.binding_ids?.length || !attempt.adapter || attempt.coverage === 'denied'
        || (attempt.adapter === 'devlake' && !connectorsEnabled)) return;
      const pending = sessionStorage.getItem(discoveryReceiptKey);
      const stored = pending ? JSON.parse(pending) as { operationId: string; revision: number; receipt: string } : null;
      const receipt = stored?.operationId === operationId && stored.revision === current.revision ? stored.receipt : productReceipt();
      sessionStorage.setItem(discoveryReceiptKey, JSON.stringify({ operationId, revision: current.revision, receipt }));
      await retryOnboardingDiscovery(productId, projectId, current.id, current.revision,
        attempt.binding_ids, attempt.adapter, attempt.cursor, receipt);
      const updated = await getOnboarding(productId, projectId, current.id);
      if (scopeRef.current !== currentScope) return;
      setSession(updated);
      sessionStorage.removeItem(discoveryReceiptKey);
    } catch {
      if (scopeRef.current === currentScope) setError('Discovery outcome is unknown. Read the saved session before retrying with the same request.');
    } finally { if (scopeRef.current === currentScope) setLoading(false); }
  }

  async function unlinkSource(bindingId: string) {
    if (!session || loading) return;
    const currentScope = storageKey;
    setLoading(true);
    setError('');
    try {
      const current = await getOnboarding(productId, projectId, session.id);
      if (scopeRef.current !== currentScope) return;
      setSession(current);
      const pending = sessionStorage.getItem(unlinkReceiptKey);
      const stored = pending ? JSON.parse(pending) as { bindingId: string; revision: number; receipt: string } : null;
      const receipt = stored?.bindingId === bindingId ? stored.receipt : productReceipt();
      const revision = stored?.bindingId === bindingId ? stored.revision : current.revision;
      sessionStorage.setItem(unlinkReceiptKey, JSON.stringify({ bindingId, revision, receipt }));
      await unlinkOnboardingSource(productId, projectId, bindingId, revision, receipt);
      const [updated, sources] = await Promise.all([getOnboarding(productId, projectId, current.id), listSourceBindings(productId, projectId)]);
      if (scopeRef.current !== currentScope) return;
      setSession(updated);
      setBindings(sources.items);
      sessionStorage.removeItem(unlinkReceiptKey);
      onSaved?.();
    } catch (failure) {
      if (scopeRef.current !== currentScope) return;
      const rejection = failure as { status?: number; detail?: { code?: string } } | null;
      if (rejection?.status === 412 && rejection.detail?.code === 'stale_revision') {
        sessionStorage.removeItem(unlinkReceiptKey);
        setError('Profile changed. Unlink was not applied. Review the saved bindings before trying again.');
        try {
          const [updated, sources] = await Promise.all([getOnboarding(productId, projectId, session.id), listSourceBindings(productId, projectId)]);
          if (scopeRef.current !== currentScope) return;
          setSession(updated);
          setBindings(sources.items);
        } catch {
          if (scopeRef.current === currentScope) setError('Profile changed. Unlink was not applied. Saved bindings are unavailable; resume onboarding before trying again.');
        }
      } else setError('Unlink outcome is unknown. Check saved bindings before retrying with the same request.');
    } finally { if (scopeRef.current === currentScope) setLoading(false); }
  }

  async function acceptSuggestedDate(proposalId: string) {
    if (!session || loading) return;
    setLoading(true);
    setError('');
    const key = `${receiptKey}:date:${proposalId}`;
    const receipt = sessionStorage.getItem(key) ?? productReceipt();
    sessionStorage.setItem(key, receipt);
    try {
      await confirmFinancialDate(productId, projectId, session.id, session.revision, proposalId, receipt);
      const saved = await getOnboarding(productId, projectId, session.id);
      const current = await getProduct(productId, projectId);
      if (scopeRef.current !== storageKey) return;
      setSession(saved);
      setGuidanceProposals(saved.proposals ?? []);
      setProfile(current);
      setReleaseDate(current.configuration?.financials?.release_target_date ?? '');
      sessionStorage.removeItem(key);
      onSaved?.();
    } catch {
      if (scopeRef.current === storageKey) setError('Could not confirm the suggested date. Check saved values before retrying.');
    } finally {
      if (scopeRef.current === storageKey) setLoading(false);
    }
  }

  const saved = loadedKey === storageKey ? session : null;
  const initiativeOptions = catalogCoverage === 'ready' && profile ? catalogInitiatives
    .filter((record) => record.tenant_id === profile.tenant_id)
    .map((record) => ({ name: record.data.name, value: referenceValue({ tenant_id: profile.tenant_id,
      project_id: projectId, source_connection_id: 'adp.catalog', record_type: 'initiative', record_id: record.id,
      record_revision: record.revision } satisfies RecordReference) })) : [];
  const selectedId = initiativeOptions.find((option) => option.value === initiative)
    ? (JSON.parse(initiative) as RecordReference).record_id : '';
  const selectedContributions = catalogCoverage === 'ready' && selectedId
    ? contributions.filter((record) => record.data.initiative_ref.record_id === selectedId && record.data.product_ref.record_id === productId)
    : [];
  const maturity = saved && ({ saved: 'Saved', getting_started: 'Getting Started', advanced: 'Advanced', pro: 'Pro' }[saved.progress.maturity] ?? saved.progress.maturity);
  return <section aria-label="Product onboarding" className="blueprint-card product-card product-financials">
    <h2 className="text-xl font-semibold">Financials</h2><p>Plan costs and a target date when you are ready. You can come back to this later.</p>
    <button type="button" className={saved ? "product-secondary" : "product-primary"} disabled={loading} onClick={() => void resume()}>
      {loading ? 'Checking saved session…' : 'Start or resume onboarding'}
    </button>
    {error && <p role="alert">{error}</p>}
    {saved && <section aria-label="Tool discovery">
      <h3>Connected tools</h3>
      {saved.discoveries?.map((item) => <div key={item.id}>
        <p>Discovery {item.id}: {item.coverage} · {item.state} · {item.complete ? 'Complete' : 'Incomplete'} · {item.resource_count} resources</p>
        {item.cursor && item.binding_ids?.length && item.coverage !== 'denied'
          && (item.adapter === 'native' || (item.adapter === 'devlake' && connectorsEnabled)) &&
          <button type="button" disabled={loading} onClick={() => void retryDiscovery(item.id)}>Retry discovery {item.id}</button>}
      </div>)}
      {bindings.filter((item) => item.status !== 'unlinked').map((item) =>
        <p key={item.id}>Binding {item.id} · {item.status} <button type="button" disabled={loading}
          onClick={() => void unlinkSource(item.id)}>Unlink binding {item.id}</button></p>)}
    </section>}
    {saved && <div className="product-financial-import">
      <button type="button" disabled={loading} onClick={() => void importDocuments()}>Suggest values from connected documents</button>
      <p className="product-muted">Review suggestions before using them. Your saved values stay unchanged until you accept a field.</p>
      {guidanceMessage && <p aria-live="polite" className="whitespace-pre-line">{guidanceMessage}</p>}
      {guidanceProposals.length > 0 && <div aria-label="Review-only suggestions">
        <p>Review suggestions; nothing has been applied automatically. Select only the date below or use a direct edit.</p>
        <ul>{guidanceProposals.map((proposal) => <li className="product-financial-suggestion" key={proposal.id}>
          {proposal.configuration?.financials?.allocations?.map(entry => <p key={entry.id}>Suggested plan: {entry.amount.amount} {entry.amount.currency} · {entry.period_start}–{entry.period_end}</p>)}
          {proposal.remaining_paths.includes('/configuration/financials/release_target_date') &&
            proposal.configuration?.financials?.release_target_date && <button type="button" disabled={loading}
              onClick={() => void acceptSuggestedDate(proposal.id)}
              aria-label={`Accept suggested date from ${proposal.id}`}>
              Accept suggested date: {proposal.configuration.financials.release_target_date}
            </button>}
          <details><summary>Suggestion details</summary><p>Proposal {proposal.id} at revision {proposal.base_revision}</p>
            <p>Remaining paths: {proposal.remaining_paths.join(', ')}</p>
            {proposal.product && Object.keys(proposal.product).length > 0 && <pre className="whitespace-pre-wrap">{JSON.stringify(proposal.product, null, 2)}</pre>}
          </details>
        </li>)}</ul>
      </div>}
    </div>}
    {saved && profile && targetRevision && <form onSubmit={(event) => void saveFinancials(event)} aria-label="Financials" className="product-financial-form">
      <h3>Financials · proposed plan</h3>
      <div className="product-financial-fields">
      <label className="product-field-wide">Name <input required value={allocation.name} onChange={(event) => setAllocation({ ...allocation, name: event.target.value })} /></label>
      <label>Amount <input required inputMode="decimal" value={allocation.amount} onChange={(event) => setAllocation({ ...allocation, amount: event.target.value })} /></label>
      <label>Currency <input required maxLength={3} value={allocation.currency} onChange={(event) => setAllocation({ ...allocation, currency: event.target.value })} /></label>
      <label className="product-field-wide">Category <select value={allocation.category} onChange={(event) => setAllocation({ ...allocation, category: event.target.value as FinancialAllocation['category'] })}><option value="ai">AI</option><option value="cloud">Cloud</option><option value="other">Other</option></select></label>
      <label>Period start <input type="date" required value={allocation.period_start} onChange={(event) => setAllocation({ ...allocation, period_start: event.target.value })} /></label>
      <label>Period end <input type="date" required value={allocation.period_end} onChange={(event) => setAllocation({ ...allocation, period_end: event.target.value })} /></label>
      </div>
      <div className="product-financial-fields">
      <label>Existing Initiative <select value={initiativeOptions.some((option) => option.value === initiative) ? initiative : ''} disabled={catalogCoverage !== 'ready'} onChange={(event) => setInitiative(event.target.value)}>
        <option value="">None</option>
        {initiativeOptions.map((option) => <option key={option.value} value={option.value}>{option.name}</option>)}
      </select></label>
      <label>Release target date <input type="date" value={releaseDate} onChange={(event) => setReleaseDate(event.target.value)} /></label>
      </div>
      {catalogCoverage === 'loading' && <p>Checking authorized Initiatives and Contributions…</p>}
      {catalogCoverage === 'unavailable' && <p role="alert">Initiative and Contribution coverage unavailable. Product editing remains available.</p>}
      {catalogCoverage === 'restricted' && <p role="alert">Initiative access changed. Optional details are hidden. Product editing remains available.</p>}
      {catalogCoverage === 'conflict' && <p role="alert">Initiative or Product revision changed. Review the latest before saving again.</p>}
      {(catalogCoverage === 'unavailable' || catalogCoverage === 'restricted' || catalogCoverage === 'conflict') && <button type="button" onClick={() => setCatalogRefresh((current) => current + 1)}>Refresh Initiative choices</button>}
      {catalogCoverage === 'ready' && selectedId && (selectedContributions.length
        ? selectedContributions.map((record) => <p key={record.id}>Contribution: {record.data.objective} · Revision {record.revision}</p>)
        : <p>No Contribution recorded for this Product and Project. Selecting an Initiative does not create one.</p>)}
      <button type="submit" className="product-primary" disabled={loading || (!!initiative && !initiativeOptions.some((option) => option.value === initiative))}>Save proposed allocation</button>
      {profile.configuration?.financials?.initiatives?.filter((ref) => initiativeOptions.some((option) => option.value === referenceValue(ref))).map((ref) => <p key={referenceValue(ref)}>Initiative: {ref.record_id}</p>)}
      {profile.configuration?.financials?.release_target_date && <p>Release target: {profile.configuration.financials.release_target_date}</p>}
      {profile.configuration?.financials?.allocations?.map((entry) => <p key={entry.id}>{entry.name}: {entry.amount.amount} {entry.amount.currency} · {entry.period_start}–{entry.period_end} · {entry.category} · {entry.approval_status}</p>)}
    </form>}
    {saved && <div className="product-financial-applicability">
      <h3>Not ready to plan costs?</h3>
      <p className="product-muted">Save this for later, or add a reason if financials do not apply.</p>
      <label>Applicability reason <input value={applicabilityReason} onChange={(event) => setApplicabilityReason(event.target.value)} /></label>
      <div className="product-financial-actions"><button type="button" disabled={loading} onClick={() => void chooseApplicability('later')}>Financials Later</button>
      <button type="button" disabled={loading || !applicabilityReason.trim()} onClick={() => void chooseApplicability('not_applicable')}>Financials Not Applicable</button></div>
    </div>}
    {saved && <details className="product-financial-guidance"><summary>Optional guidance</summary>
      <form onSubmit={(event) => void askGuidance(event)}>
        <label>Ask for guidance <input value={guidanceInput} maxLength={2000} onChange={(event) => setGuidanceInput(event.target.value)} /></label>
        <label className="product-checkbox-label"><input type="checkbox" checked={meteredGuidance} onChange={(event) => setMeteredGuidance(event.target.checked)} /> Use metered model assistance (may incur a charge)</label>
        <button type="submit" disabled={loading || !guidanceInput.trim()}>Suggest next steps</button>
      </form>
    </details>}
    {saved && <div role="status" className="product-financial-session">
      <details><summary>Saved session details</summary>
        <p>Session {saved.id} · Revision {saved.revision} · {saved.state}</p>
        <p>{saved.progress.completed}/{saved.progress.applicable} checks · {saved.progress.percentage}% · {maturity}</p>
        <ul>{(['information', 'basic', 'advanced', 'financials'] as const).map((name) =>
          <li key={name}>{name}: {saved.steps[name]?.status ?? 'not_started'}</li>)}</ul>
      </details>
    </div>}
  </section>;
}
