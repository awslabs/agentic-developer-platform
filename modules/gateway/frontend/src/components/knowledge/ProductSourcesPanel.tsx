import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { ProviderIcon } from '@/components/ui/ProviderIcon';
import { includeProductKnowledgeSource, listProductKnowledgeSources, updateKnowledgeRefresh, type ProductKnowledgeSource } from '@/services/knowledge';

function errorMessage(error: unknown) {
  const detail = (error as { detail?: { message?: string } | string })?.detail;
  return typeof detail === 'string' ? detail : detail?.message || (error instanceof Error ? error.message : 'Could not update Product sources. Please retry.');
}

export function ProductSourcesPanel({ productId, onChanged }: { productId: string; onChanged: () => void }) {
  const [items, setItems] = useState<ProductKnowledgeSource[]>([]);
  const [after, setAfter] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [pendingEnabled, setPendingEnabled] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const [choices, setChoices] = useState<Record<string, boolean>>({});
  const active = useRef(true);
  const version = useRef(0);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; version.current += 1; };
  }, []);

  const load = useCallback(async (cursor?: string) => {
    const request = ++version.current;
    setLoading(true);
    setError('');
    try {
      const result = await listProductKnowledgeSources(productId, cursor);
      if (!active.current || version.current !== request) return;
      const incoming = result.items;
      setItems(previous => cursor ? [...previous, ...incoming.filter(item => !previous.some(old => old.binding_id === item.binding_id))] : incoming);
      setAfter(result.after);
    } catch (failure) {
      if (active.current && version.current === request) {
        setItems([]);
        setAfter(null);
        setError(errorMessage(failure));
      }
    } finally {
      if (active.current && version.current === request) setLoading(false);
    }
  }, [productId]);

  useEffect(() => { void load(); }, [load]);

  async function change(source: ProductKnowledgeSource, enabled: boolean, reconnect = false) {
    if (busy) return;
    setBusy(source.binding_id);
    setPendingEnabled(enabled);
    setError('');
    try {
      if (!reconnect && source.included && source.asset_id && source.revision) {
        await updateKnowledgeRefresh(source.asset_id, enabled, source.revision);
      } else if (source.source_ref) {
        await includeProductKnowledgeSource(productId, source.binding_id, source.source_ref, enabled);
      } else throw new Error('Refresh source choices before adding this repository.');
      if (!active.current) return;
      onChanged();
      await load();
    } catch (failure) {
      if (active.current) setError(errorMessage(failure));
    } finally {
      if (active.current) { setBusy(null); setPendingEnabled(null); }
    }
  }

  return <section aria-label="Product repositories">
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-base font-semibold text-gray-900 dark:text-white">
            <ProviderIcon provider="github" className="h-5 w-5" /> Product repositories
          </h3>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
            Include saved repositories in Product Context. Daily updates use your organization’s ADP GitHub App.
          </p>
        </div>
        <Button variant="ghost" size="sm" disabled={loading || !!busy} onClick={() => void load()}>Refresh sources</Button>
      </div>
      {loading && <p role="status" className="mt-4 text-sm text-gray-500">Loading Product sources…</p>}
      {error && <p role="alert" className="mt-4 text-sm text-red-700 dark:text-red-300">{error}</p>}
      {!loading && !error && !items.length && <p className="mt-4 text-sm text-gray-600 dark:text-gray-400">
        No saved GitHub repositories. Choose a repository under Connections in the product profile.
        {' '}<Link className="text-primary-600 underline dark:text-primary-400" to="/settings/connections">Manage GitHub connections</Link>
      </p>}
      <ul className="mt-4 divide-y divide-gray-200 dark:divide-gray-700">
        {items.map(source => {
          const name = source.name || 'Saved repository';
          const enabled = source.included
            ? busy === source.binding_id && pendingEnabled !== null ? pendingEnabled : !!source.keep_updated
            : !!choices[source.binding_id];
          return <li key={source.binding_id} className="flex flex-col gap-3 py-4 first:pt-0 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 space-y-1">
              <p className="break-words font-medium text-gray-900 dark:text-white">{name}</p>
              {source.connection_name && <p className="text-sm text-gray-600 dark:text-gray-400">Connection: {source.connection_name} · ADP GitHub App</p>}
              {source.project_name && <p className="text-sm text-gray-500">Project: {source.project_name}</p>}
              {source.included && <p className="text-sm text-gray-600 dark:text-gray-400">
                In Context · {source.status?.replace(/_/g, ' ')} · {enabled ? 'Daily updates enabled' : 'Automatic updates off'}
              </p>}
              {source.included && <p className="text-sm text-gray-500">Last successful refresh: {source.last_refreshed_at ? new Date(source.last_refreshed_at).toLocaleString() : 'Not yet completed'}</p>}
              {source.error && <p className="text-sm text-amber-700 dark:text-amber-300">{source.error}</p>}
            </div>
            <div className="flex shrink-0 flex-col gap-2">
              <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                <input type="checkbox" checked={enabled} aria-label={`Keep updated: ${name}`}
                  disabled={!!busy || (source.included ? !source.can_manage || (!source.eligible && !enabled) : !source.eligible)}
                  onChange={event => source.included ? void change(source, event.target.checked)
                    : setChoices(previous => ({ ...previous, [source.binding_id]: event.target.checked }))} />
                Keep updated daily
              </label>
              {!source.included && <Button size="sm" variant="secondary" disabled={!!busy || !source.eligible}
                onClick={() => void change(source, enabled)} aria-label={`Include ${name} in Context`}>
                {busy === source.binding_id ? 'Adding…' : 'Include in Context'}
              </Button>}
              {source.included && source.needs_reconnect && source.can_manage && <Button size="sm" variant="secondary" disabled={!!busy}
                onClick={() => void change(source, enabled, true)} aria-label={`Reconnect ${name}`}>
                Reconnect source
              </Button>}
              {source.included && busy === source.binding_id && <p role="status" className="text-sm text-gray-500">Saving…</p>}
            </div>
          </li>;
        })}
      </ul>
      {after && <Button variant="secondary" size="sm" disabled={loading || !!busy} onClick={() => void load(after)}>More Product sources</Button>}
    </Card>
  </section>;
}
