import { createContext, useEffect, useState } from 'react';
import { inspectProductSource, listProductSources, type ProductSource, type RecordReference } from '@/services/products';

export const sourceReferenceKey = (ref: RecordReference) => JSON.stringify([
  ref.tenant_id, ref.project_id, ref.source_connection_id, ref.record_type, ref.record_id, ref.record_revision,
]);

const repositoryId = (ref: RecordReference) => ref.record_type === 'repository_commit' ? ref.record_id
  : ref.record_type === 'document' && /^[0-9]+:/.test(ref.record_id) ? ref.record_id.split(':')[0] : null;

export const ProductSourceNameContext = createContext<(ref: RecordReference) => string>(ref => ref.record_id);

/** Display metadata only. Never replace the immutable references used to save or run. */
export default function useProductSourceLabels(productId: string, projectId: string, tenantId: string | undefined, references: RecordReference[], refreshToken?: object | null) {
  const requestKey = JSON.stringify([productId, projectId, tenantId, references]);
  const [state, setState] = useState<{ key: string; labels: Record<string, string>; loading: boolean }>({ key: '', labels: {}, loading: false });
  useEffect(() => {
    let active = true;
    const [product, project, tenant, refs] = JSON.parse(requestKey) as [string, string, string | null, RecordReference[]];
    const targets = refs.filter(ref => tenant && ref.tenant_id === tenant && ref.project_id === project
      && (ref.record_type === 'connection' || repositoryId(ref)));
    const labels: Record<string, string> = {};
    const publish = (loading: boolean) => { if (active) setState({ key: requestKey, labels: { ...labels }, loading }); };
    publish(targets.length > 0);
    if (!targets.length) return () => { active = false; };

    async function inspect(connection: ProductSource) {
      const matching = targets.filter(ref => ref.source_connection_id === connection.connection_ref.id);
      const remaining = new Set(matching.map(repositoryId).filter((id): id is string => id !== null));
      const accountRefs = matching.filter(ref => ref.record_type === 'connection' && ref.record_id === connection.connection_ref.id);
      const accountLabel = (account?: string) => `${account ?? 'Account ID unavailable'}${connection.label ? ` · ${connection.label}` : ''}`;
      if (connection.provider === 'aws') accountRefs.forEach(ref => { labels[sourceReferenceKey(ref)] = accountLabel(); });
      publish(true);
      let cursor: string | undefined;
      const cursors = new Set<string>();
      do {
        const page = await inspectProductSource(product, project, connection.connection_ref, cursor);
        if (!active) return;
        const item = page.items.find(item => item.connection_ref.id === connection.connection_ref.id
          && item.connection_ref.kind === connection.connection_ref.kind && item.provider === connection.provider);
        if (!item || !['complete', 'partial'].includes(item.coverage)) return;
        const resources = item.resources ?? [];
        if (connection.provider === 'aws') {
          accountRefs.forEach(ref => {
            const accounts = new Set(resources.filter(resource => resource.kind === 'environment'
              && resource.source_revision === ref.record_revision && /^[0-9]{12}$/.test(resource.verified_environment?.account_id ?? ''))
              .map(resource => resource.verified_environment!.account_id));
            if (accounts.size === 1) labels[sourceReferenceKey(ref)] = accountLabel([...accounts][0]);
          });
        } else if (['github', 'gitlab'].includes(connection.provider)) {
          resources.forEach(resource => {
            if (resource.kind !== 'repository' || !remaining.has(resource.native_id) || !resource.full_name?.includes('/')) return;
            matching.filter(ref => repositoryId(ref) === resource.native_id).forEach(ref => {
              labels[sourceReferenceKey(ref)] = ref.record_type === 'document'
                ? `${resource.full_name} / ${ref.record_id.slice(resource.native_id.length + 1)}` : resource.full_name!;
            });
            remaining.delete(resource.native_id);
          });
        }
        publish(true);
        cursor = item.resource_cursor;
        if (!remaining.size || !cursor || cursors.has(cursor)) break;
        cursors.add(cursor);
      } while (active);
    }

    void (async () => {
      let cursor: string | undefined;
      const cursors = new Set<string>();
      const seen = new Set<string>();
      const remaining = new Set(targets.map(ref => ref.source_connection_id));
      do {
        const page = await listProductSources(product, project, cursor);
        if (!active) return;
        const selected = page.items.filter(item => remaining.has(item.connection_ref.id) && !seen.has(item.connection_ref.id));
        selected.forEach(item => { seen.add(item.connection_ref.id); remaining.delete(item.connection_ref.id); });
        // One failed metadata lookup must not hide the other available choices.
        await Promise.allSettled(selected.map(inspect));
        cursor = page.next_cursor ?? undefined;
        if (!remaining.size || !cursor || cursors.has(cursor)) break;
        cursors.add(cursor);
      } while (active);
    })().catch(() => { /* Keep explicit fallback labels if metadata cannot be read. */ }).finally(() => publish(false));
    return () => { active = false; };
  }, [requestKey, refreshToken]);

  const current: { labels: Record<string, string>; loading: boolean } = state.key === requestKey ? state : { labels: {}, loading: references.length > 0 };
  return {
    sourceName: (ref: RecordReference) => current.labels[sourceReferenceKey(ref)]
      ?? (ref.record_type === 'repository_commit' && /^[0-9]+$/.test(ref.record_id)
        ? `${current.loading ? 'Loading repository name…' : 'Repository name unavailable'} · ID ${ref.record_id}` : ref.record_id),
    accountName: (ref: RecordReference) => current.labels[sourceReferenceKey(ref)]
      ?? `${current.loading ? 'Loading AWS account…' : 'Account ID unavailable'} · Connection ${ref.record_id.slice(0, 8)}`,
  };
}
