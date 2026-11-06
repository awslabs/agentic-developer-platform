import { useCallback, useEffect, useRef, useState } from 'react';
import { addPortfolioMembership, closePortfolioMembership, listPortfolioMemberships, listPortfolios, type PortfolioMembership, type PortfolioRecord, type ProductProfile, type RecordReference } from '@/services/products';

type Props = { productId: string; projectId: string; profile: ProductProfile };
type Membership = { portfolio: PortfolioRecord; relationship: PortfolioMembership };
type Coverage = 'loading' | 'ready' | 'unavailable' | 'restricted' | 'conflict';

export default function ProductOrganization({ productId, projectId, profile }: Props) {
  const scope = `${profile.tenant_id}:${productId}:${projectId}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const [portfolios, setPortfolios] = useState<PortfolioRecord[]>([]);
  const [memberships, setMemberships] = useState<Membership[]>([]);
  const [canEdit, setCanEdit] = useState(false);
  const [coverage, setCoverage] = useState<Coverage>('loading');
  const [selected, setSelected] = useState('');
  const [validFrom, setValidFrom] = useState('');
  const [validTo, setValidTo] = useState('');
  const [confirmedStart, setConfirmedStart] = useState('');
  const [busy, setBusy] = useState(false);

  const clear = useCallback((status: Coverage) => {
    setPortfolios([]);
    setMemberships([]);
    setSelected('');
    setConfirmedStart('');
    setCanEdit(false);
    setCoverage(status);
  }, []);

  const load = useCallback(async () => {
    setCoverage('loading');
    setCanEdit(false);
    setConfirmedStart('');
    try {
      const records: PortfolioRecord[] = [];
      const visible: Membership[] = [];
      let cursor: string | undefined;
      let editable = false;
      do {
        const page = await listPortfolios(cursor);
        editable = page.can_edit === true;
        records.push(...page.items);
        cursor = page.next_cursor;
      } while (cursor);
      for (const portfolio of records) {
        let next: string | undefined;
        do {
          const page = await listPortfolioMemberships(portfolio.id, next);
          visible.push(...page.items.filter((relationship) => relationship.product_ref.record_id === productId
            && relationship.product_ref.project_id === projectId).map((relationship) => ({ portfolio, relationship })));
          next = page.next_cursor;
        } while (next);
      }
      if (scopeRef.current !== scope) return;
      setPortfolios(records);
      setMemberships(visible);
      setCanEdit(editable);
      setCoverage('ready');
      return visible;
    } catch (failure) {
      if (scopeRef.current !== scope) return;
      const status = (failure as { status?: number }).status;
      clear(status === 403 || status === 404 ? 'restricted'
        : status === 400 || status === 409 || status === 412 ? 'conflict' : 'unavailable');
      return null;
    }
  }, [scope, productId, projectId, clear]);

  useEffect(() => { void load(); }, [load]);

  async function add(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const portfolio = portfolios.find((record) => record.id === selected);
    if (!canEdit || !portfolio || !validFrom || busy) return;
    const portfolioRef: RecordReference = { tenant_id: profile.tenant_id, project_id: projectId,
      source_connection_id: 'adp.catalog', record_type: 'portfolio', record_id: portfolio.id, record_revision: portfolio.revision };
    const productRef: RecordReference = { tenant_id: profile.tenant_id, project_id: projectId,
      source_connection_id: 'adp.profile', record_type: 'product', record_id: productId, record_revision: String(profile.revision) };
    setBusy(true);
    try {
      const effectiveAt = `${validFrom}T00:00:00Z`;
      const saved = await addPortfolioMembership(portfolioRef, productRef, effectiveAt);
      let cursor: string | undefined;
      let confirmed = false;
      do {
        const page = await listPortfolioMemberships(portfolio.id, cursor, effectiveAt);
        confirmed = page.items.some((relationship) => relationship.relationship_id === saved.id
          && relationship.relationship_revision === saved.revision && relationship.product_ref.record_id === productId
          && relationship.product_ref.project_id === projectId);
        cursor = page.next_cursor;
      } while (cursor && !confirmed);
      if (scopeRef.current !== scope) return;
      if (!confirmed) { clear('conflict'); return; }
      const readback = await load();
      if (scopeRef.current === scope && readback) {
        const current = readback.find(({ relationship }) => relationship.relationship_id === saved.id);
        if (!current || current.relationship.relationship_revision === saved.revision) {
          if (!current) setConfirmedStart(validFrom);
          setSelected('');
          setValidFrom('');
        } else clear('conflict');
      }
    } catch (failure) {
      if (scopeRef.current === scope) {
        const status = (failure as { status?: number }).status;
        clear(status === 403 || status === 404 ? 'restricted'
          : status === 400 || status === 409 || status === 412 ? 'conflict' : 'unavailable');
      }
    } finally {
      if (scopeRef.current === scope) setBusy(false);
    }
  }

  async function close(membership: Membership) {
    if (!canEdit || !validTo || busy) return;
    setBusy(true);
    try {
      const saved = await closePortfolioMembership(membership.portfolio.id, membership.relationship, `${validTo}T00:00:00Z`);
      const readback = await load();
      if (scopeRef.current === scope) {
        if (readback && (!readback.some(({ relationship }) => relationship.relationship_id === saved.id)
          || readback.some(({ relationship }) => relationship.relationship_id === saved.id && relationship.relationship_revision === saved.revision))) {
          setValidTo('');
        } else if (readback) clear('conflict');
      }
    } catch (failure) {
      if (scopeRef.current === scope) {
        const status = (failure as { status?: number }).status;
        clear(status === 403 || status === 404 ? 'restricted'
          : status === 409 || status === 412 ? 'conflict' : 'unavailable');
      }
    } finally {
      if (scopeRef.current === scope) setBusy(false);
    }
  }

  const available = portfolios.filter((portfolio) => !memberships.some((membership) => membership.portfolio.id === portfolio.id));
  return <section aria-label="Portfolio organization" className="space-y-4 pt-4">
    <h2 className="text-xl font-semibold">Portfolio organization</h2>
    <p>Portfolios are optional collections. They do not change Product or Project access.</p>
    {coverage === 'loading' && <p role="status">Loading authorized Portfolios…</p>}
    {coverage === 'unavailable' && <p role="alert">Portfolio coverage unavailable. Product editing is still available. <button className="underline" onClick={() => void load()}>Retry</button></p>}
    {coverage === 'restricted' && <p role="alert">Portfolio access changed. Optional details are hidden. Product editing is still available. <button className="underline" onClick={() => void load()}>Check access again</button></p>}
    {coverage === 'conflict' && <p role="alert">Portfolio details changed elsewhere. No change was confirmed. <button className="underline" onClick={() => void load()}>Refresh current Portfolios</button></p>}
    {coverage === 'ready' && <>
      {confirmedStart && <p role="status">Membership confirmed from {confirmedStart}. It is not in the current membership list.</p>}
      {memberships.length === 0 && !confirmedStart && <p>No visible Portfolio memberships for this Product.</p>}
      <ul>{memberships.map((membership) => <li key={membership.relationship.relationship_id} className="space-y-2 border-b border-gray-200 py-4 last:border-0">
        <span>{membership.portfolio.data.name}: {membership.relationship.valid_from.slice(0, 10)} to {membership.relationship.valid_to?.slice(0, 10) ?? 'ongoing'}</span>
        {canEdit && <div><label>End membership on <input type="date" value={validTo} onChange={(event) => setValidTo(event.target.value)} /></label>
          <button className="ml-2 underline" disabled={busy || !validTo || validTo <= membership.relationship.valid_from.slice(0, 10)} onClick={() => void close(membership)}>End {membership.portfolio.data.name} membership</button></div>}
      </li>)}</ul>
      {canEdit && available.length > 0 && <form className="space-y-2" onSubmit={(event) => void add(event)}>
        <label className="block">Add to Portfolio <select value={selected} onChange={(event) => setSelected(event.target.value)}>
          <option value="">Choose a Portfolio</option>
          {available.map((portfolio) => <option key={portfolio.id} value={portfolio.id}>{portfolio.data.name}</option>)}
        </select></label>
        <label className="block">Membership starts on <input type="date" value={validFrom} onChange={(event) => setValidFrom(event.target.value)} /></label>
        <button disabled={!selected || !validFrom || busy}>Add membership</button>
      </form>}
      {!canEdit && <p>Portfolio membership is read-only with your current access.</p>}
    </>}
  </section>;
}
