import { useEffect, useState, type ReactNode } from 'react';
import { Button, Modal } from '@/components/ui';
import { inspectProductPipelines, type ProductSource, type SourceResource } from '@/services/products';

type Props = {
  productId: string;
  projectId: string;
  connection: ProductSource;
  repository: SourceResource;
  busy: boolean;
  pending: boolean;
  feedback: ReactNode;
  isLinked: (resource: SourceResource) => boolean;
  onLink: (source: ProductSource, resource: SourceResource) => void;
  onClose: () => void;
};

const PAGE_SIZE = 10;

export default function ProductWorkflowPicker({ productId, projectId, connection, repository, busy, pending, feedback, isLinked, onLink, onClose }: Props) {
  const [resources, setResources] = useState<SourceResource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const { kind: connectionKind, id: connectionId } = connection.connection_ref;

  useEffect(() => {
    let active = true;
    setResources([]);
    setLoading(true);
    setError('');
    setPage(0);
    const read = async () => {
      const found = new Map<string, SourceResource>();
      const cursors = new Set<string>();
      let cursor: string | undefined;
      try {
        do {
          const result = await inspectProductPipelines(productId, projectId, { kind: connectionKind, id: connectionId }, repository.native_id, cursor);
          if (!active) return;
          const source = result.items[0];
          if (!source || source.connection_ref.kind !== connectionKind || source.connection_ref.id !== connectionId
            || source.coverage === 'denied' || source.coverage === 'unavailable') throw new Error('Discovery unavailable');
          for (const resource of source.resources ?? []) {
            if (resource.kind !== 'pipeline' || resource.repository_id !== repository.native_id) throw new Error('Repository changed');
            found.set(resource.native_id, resource);
          }
          setResources([...found.values()]);
          cursor = source.resource_cursor;
          if (cursor && cursors.has(cursor)) throw new Error('Repeated discovery page');
          if (cursor) cursors.add(cursor);
        } while (cursor && active);
      } catch {
        if (active) {
          setResources([]);
          setError('Could not read the workflows for this repository. Check the connection or retry.');
        }
      } finally {
        if (active) setLoading(false);
      }
    };
    void read();
    return () => { active = false; };
  }, [productId, projectId, connectionKind, connectionId, repository.native_id, attempt]);

  const search = query.trim().toLocaleLowerCase();
  const filtered = resources.filter(resource => `${resource.name} ${resource.native_id}`.toLocaleLowerCase().includes(search));
  const lastPage = Math.max(0, Math.ceil(filtered.length / PAGE_SIZE) - 1);
  const currentPage = Math.min(page, lastPage);
  const visible = filtered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

  return <Modal isOpen onClose={() => { if (!busy) onClose(); }} title={`Workflows in ${repository.name}`} size="xl">
    <div className="product-workspace product-workflow-picker space-y-4">
      <p className="product-muted">Optionally link the GitHub Actions workflows this project uses. Linking saves a reference; running or deploying is a separate action.</p>
      <label>Search workflows<input type="search" value={query} placeholder="Workflow name or ID" onChange={event => { setQuery(event.target.value); setPage(0); }} /></label>
      {loading && <p role="status">Loading workflows… {resources.length > 0 && `${resources.length} found so far.`}</p>}
      {error && <div role="alert">{error} <Button variant="secondary" onClick={() => setAttempt(value => value + 1)}>Retry</Button></div>}
      {!loading && !error && !resources.length && <p>No workflows are available in this repository.</p>}
      {!loading && !error && resources.length > 0 && !filtered.length && <p>No workflows match your search.</p>}
      <ul className="product-workflow-list" aria-label="Repository workflows">
        {visible.map(resource => <li key={resource.native_id}>
          <div><strong>{resource.name}</strong><span className="product-muted">Workflow {resource.native_id}</span></div>
          <Button variant="secondary" disabled={busy || pending || isLinked(resource)} onClick={() => onLink({ ...connection, resources }, resource)}>
            {isLinked(resource) ? 'Linked' : 'Link to project'}
          </Button>
        </li>)}
      </ul>
      {filtered.length > 0 && <nav className="product-list-pagination" aria-label="Workflow pages">
        <span className="product-muted">{currentPage * PAGE_SIZE + 1}–{Math.min((currentPage + 1) * PAGE_SIZE, filtered.length)} of {filtered.length}{loading ? ' loaded' : filtered.length === 1 ? ' workflow' : ' workflows'}</span>
        {filtered.length > PAGE_SIZE && <><Button variant="secondary" disabled={currentPage === 0 || busy} onClick={() => setPage(currentPage - 1)}>Previous</Button>
        <Button variant="secondary" disabled={currentPage >= lastPage || busy} onClick={() => setPage(currentPage + 1)}>Next</Button></>}
      </nav>}
      {feedback}
      <div className="flex justify-end"><Button disabled={busy} onClick={onClose}>Done</Button></div>
    </div>
  </Modal>;
}
