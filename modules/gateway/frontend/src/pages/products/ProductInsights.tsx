import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import { Modal } from '@/components/ui';
import { getBusinessContextEvidence } from '@/services/engineeringInsights';
import { listSourceBindings } from '@/services/products';
import { workspacePath } from './productWorkspaceNavigation';

const reports = {
  delivery: { title: 'Delivery performance', source: 'DevLake', description: 'PR cycle time, throughput and production delivery metrics.', metrics: [
    ['PR cycle time', 'Median time from PR creation to merge. Requires collected PR timestamps and declared source coverage. This does not measure time to production.'],
    ['Merged PR throughput', 'Distinct PRs merged in a reporting period. Requires a complete, deduplicated source population.'],
    ['Deployment frequency', 'Eligible successful production deployments per reporting period. Requires a production environment mapping and deployment feed.'],
    ['Change failure rate', 'Failed production deployments divided by eligible deployments. Requires causal incident links and a completed 7-day observation window.'],
  ] },
  quality: { title: 'Quality & acceptance', source: 'Engineering Insights', description: 'Requirements coverage, test execution and acceptance evidence.', metrics: [
    ['Acceptance criteria with passing evidence', 'Applicable criteria with passing test evidence at the required revision. Requires versioned criteria, test mappings and execution results.'],
    ['Acceptance evidence gaps', 'Missing test mappings, missing execution evidence and failing results. Unverified criteria remain visible and do not count as passed.'],
    ['Flaky tests', 'Inconsistent outcomes across repeated executions of the same test identity and revision. Requires retained retries and repeated attempts.'],
  ] },
  outcomes: { title: 'Engineering outcomes', source: 'Engineering Insights', description: 'Delivery, production quality, rework, cost and human effort.', metrics: [
    ['Lead time per story', 'Earliest linked commit to the first successful production deployment completing the accepted story. Requires story, commit and deployment lineage.'],
    ['Stories shipped per team per week', 'Distinct stories first completed in production per reporting week. Requires dated team ownership and accepted completion.'],
    ['Change failure rate', 'Failed eligible production deployments divided by eligible deployments, after the full 7-day observation window. Requires causal failure links.'],
    ['Rework rate', 'Shipped stories requiring a corrective fix or revert within 30 days, divided by eligible shipped stories with a complete observation window.'],
    ['Total cost per story', 'All in-scope team delivery costs, including labor and unfinished work, divided by stories first shipped. Requires reconciled costs and approved allocations.'],
    ['Human effort per shipped story', 'Calibrated active team delivery hours divided by stories first shipped. Unattended agent runtime is excluded. Requires effort evidence and explicit coverage.'],
  ] },
};
type Report = keyof typeof reports;
type Props = { productId: string; projectId: string };

function BusinessEvidence({ productId, projectId }: Props) {
  const { user } = useAuth();
  const [asOf, setAsOf] = useState(() => new Date().toISOString());
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors[cursors.length - 1];
  const query = useQuery({ queryKey: ['engineering-insights', user?.orgId, user?.id, productId, projectId, asOf, cursor],
    queryFn: ({ signal }) => getBusinessContextEvidence(productId, projectId, asOf, cursor, signal), retry: false,
    staleTime: 0, gcTime: 0 });
  const status = (query.error as { status?: number } | null)?.status;
  return <section className="product-lifecycle"><h3>Business-context evidence</h3>
    <p className="product-muted">Published work and cost associations for this product and project. These records do not establish the outcome metrics above.</p>
    {query.isPending ? <p role="status">Loading published evidence…</p> : query.isError ? <div role="alert"><p>{status === 404 || status === 403
      ? 'Published evidence is unavailable for this scope or your current access. Ask the product owner to check publication and source access.'
      : status === 409 ? 'The evidence snapshot or its authorization is incomplete. Ask the Insights owner to reconcile and republish it, then retry.'
        : status === 501 ? 'This evidence view is not enabled in this deployment. Ask the platform owner to enable Engineering Insights results.'
          : 'Could not load a valid evidence snapshot. Retry; if this persists, ask the Insights owner to check the results service.'}</p><button className="product-action-link" onClick={() => void query.refetch()}>Retry evidence</button></div>
      : query.data && <>
        <dl className="product-evidence-fields"><div><dt>Snapshot requested</dt><dd>{new Date(query.data.as_of).toLocaleString()}</dd></div><div><dt>Dataset</dt><dd>{query.data.dataset_version}</dd></div><div><dt>Coverage</dt><dd>Complete for this published view</dd></div><div><dt>Mapped work items</dt><dd>{query.data.work_count}</dd></div>
          <div><dt>Recorded costs</dt><dd>{Object.entries(query.data.cost_by_currency).map(([currency, value]) => `${value} ${currency}`).join(' · ') || 'No cost evidence in this snapshot'}</dd></div></dl>
        <details><summary>Source records &amp; mapping versions</summary><p>{query.data.mapping_versions.join(', ')}</p><ul>{query.data.contexts.map((context, index) => <li key={index}>{context.subject_ref.record_type} · {context.subject_ref.record_id} · revision {context.subject_ref.record_revision}</li>)}</ul></details>
        <div className="product-header-links"><button disabled={!cursors.length || query.isFetching} onClick={() => setCursors(items => items.slice(0, -1))}>Previous records</button><button disabled={!query.data.next_cursor || query.isFetching} onClick={() => setCursors(items => [...items, query.data!.next_cursor!])}>Next records</button></div>
      </>}
    <button className="product-action-link" onClick={() => { setCursors([]); setAsOf(new Date().toISOString()); }}>Check latest publication</button>
  </section>;
}

function Sources({ productId, projectId }: Props) {
  const { user } = useAuth();
  const query = useQuery({ queryKey: ['insight-source-bindings', user?.orgId, user?.id, productId, projectId], queryFn: () => listSourceBindings(productId, projectId), retry: false, gcTime: 0 });
  return <div className="product-lifecycle"><p>Connections come from Product profile. Selecting a connection does not authorize historical collection or confirm metric coverage.</p>
    {query.isPending ? <p role="status">Loading saved sources…</p> : query.isError ? <p role="alert">Could not load saved sources. <button onClick={() => void query.refetch()}>Retry sources</button></p>
      : <ul>{query.data?.items.length ? query.data.items.map(binding => <li className="product-activity-row" key={binding.id}><div><h3>{binding.selection?.provider ?? 'Saved source'} · {binding.selection?.kind ?? 'Source'}</h3><p>{binding.selection?.namespace} {binding.selection?.native_id}</p><p className="product-muted">Binding status: {binding.status}. Analytics collection not reported by this view.</p></div></li>) : <li>No source bindings are saved for this project.</li>}</ul>}
    <Link className="product-action-link" to={`${workspacePath(productId, projectId, 'profile')}#product-connections`}>Review product connections</Link></div>;
}

export default function ProductInsights(props: Props) {
  const [report, setReport] = useState<Report | null>(null);
  const [metric, setMetric] = useState<number | null>(null);
  const [sources, setSources] = useState(false);
  const [evidence, setEvidence] = useState(false);
  const selected = report ? reports[report] : null;
  return <section className="blueprint-card product-card product-lifecycle" aria-label="Product insights">
    <header className="product-section-heading"><div><h2>Insights</h2><p className="product-muted">Understand delivery performance, quality and engineering outcomes.</p></div><button className="product-action-link" onClick={() => setSources(true)}>Data sources &amp; coverage</button></header>
    <p className="product-muted">Team-level reporting · Current product and project</p>
    <ul>{Object.entries(reports).map(([id, entry]) => <li className="product-activity-row" key={id}><div><h3>{entry.title}</h3><p className="product-muted">{entry.description}</p><p className="product-muted">{entry.source} · Metrics not available in this UI yet</p></div><button className="product-action-link" onClick={() => { setReport(id as Report); setMetric(null); setEvidence(false); }}>View report</button></li>)}</ul>
    <details><summary>Where these insights come from</summary><p>DevLake collects engineering-tool data. Engineering Insights links requirements, tests, runs and delivery evidence. Metrics need qualified source events, definitions and coverage before values can be shown.</p></details>
    <Modal isOpen={sources} onClose={() => setSources(false)} title="Data sources & coverage" size="xl">{sources && <div className="product-workspace"><Sources {...props} /></div>}</Modal>
    <Modal isOpen={!!selected} onClose={() => { setReport(null); setMetric(null); setEvidence(false); }} title={selected ? metric === null ? selected.title : selected.metrics[metric][0] : ''} size="xl">
      {selected && <div className="product-workspace product-lifecycle"><p className="product-muted">{selected.source} · Current product and project</p>
        {metric === null ? <><p>Metrics are not available in this UI yet. Review the definitions and required evidence below.</p><ul>{selected.metrics.map(([name], index) => <li className="product-activity-row" key={name}><div><h3>{name}</h3><span className="product-availability">Awaiting metric integration</span></div><button className="product-action-link" onClick={() => setMetric(index)}>Definition &amp; evidence</button></li>)}</ul>
          {report === 'outcomes' && <><button className="product-action-link" onClick={() => setEvidence(value => !value)} aria-expanded={evidence}>Inspect published business-context evidence</button>{evidence && <BusinessEvidence {...props} />}</>}
        </> : <><p>{selected.metrics[metric][1]}</p><dl className="product-evidence-fields"><div><dt>Provenance</dt><dd>{selected.source}</dd></div><div><dt>Freshness &amp; coverage</dt><dd>No qualified metric dataset is exposed by this UI yet.</dd></div><div><dt>Next step</dt><dd>Review connected sources with the Insights owner and enable the metric’s collection and reporting integration.</dd></div></dl><button className="product-action-link" onClick={() => setMetric(null)}>← Back to report</button></>}
      </div>}
    </Modal>
  </section>;
}
