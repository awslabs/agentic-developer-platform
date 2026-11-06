/** Reuse existing imports and connections; sharing comes from the containing screen. */
import { useState, useEffect, useRef } from 'react';
import { Modal, ModalFooter } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { BulkUploadDialog } from './BulkUploadDialog';
import { getAccessibleRepos, createAsset } from '@/services/knowledge';
import type { AccessibleRepo } from '@/types';

export interface AddAssetDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onAssetAdded: () => void;
  scope: 'personal' | 'product';
  productId?: string;
}

function sourceDetails(value: string) {
  const source = value.trim();
  if (/^s3:\/\/[^/\s]+\/[^\s]+$/.test(source)) return { asset_type: 'doc', source_ref: source };
  const github = source.match(/^(?:https:\/\/github\.com\/|git@github\.com:)([^/\s]+)\/([^/?#\s]+)\/?$/i);
  if (github) {
    const name = `${github[1]}/${github[2].replace(/\.git$/, '')}`;
    return { asset_type: 'repo', source_ref: `https://github.com/${name}`, display_name: name };
  }
  try {
    const url = new URL(source);
    if (['https:', 'http:'].includes(url.protocol) && url.hostname && !url.username && !url.password) {
      return { asset_type: 'url', source_ref: source };
    }
  } catch { /* Show one actionable validation error below. */ }
  throw new Error('Enter a web link, GitHub repository URL, or S3 document URI.');
}

export function AddAssetDialog(props: AddAssetDialogProps) {
  // Closing or changing Product discards the form and outstanding picker results.
  return props.isOpen ? <AddInformationForm key={`${props.scope}:${props.productId ?? ''}`} {...props} /> : null;
}

function AddInformationForm({ onClose, onAssetAdded, scope, productId }: AddAssetDialogProps) {
  const [source, setSource] = useState('');
  const [showBulk, setShowBulk] = useState(false);
  const [name, setName] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [repos, setRepos] = useState<AccessibleRepo[]>([]);
  const [repoSearch, setRepoSearch] = useState('');
  const [repoPage, setRepoPage] = useState(1);
  const [hasMoreRepos, setHasMoreRepos] = useState(false);
  const [reposLoading, setReposLoading] = useState(false);
  const [repoError, setRepoError] = useState('');
  const active = useRef(true);

  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  useEffect(() => {
    if (!pickerOpen) return;
    let cancelled = false;
    setReposLoading(true);
    const timer = setTimeout(() => {
      void getAccessibleRepos({ search: repoSearch.trim() || undefined, page: repoPage, pageSize: 20 })
        .then((response) => {
          if (cancelled) return;
          setRepos((previous) => repoPage === 1 ? response.repos : [...previous, ...response.repos]);
          setHasMoreRepos(response.hasMore);
          setRepoError('');
        })
        .catch(() => {
          if (!cancelled) setRepoError('Could not load connected repositories. Try searching again or paste a link.');
        })
        .finally(() => { if (!cancelled) setReposLoading(false); });
    }, repoSearch ? 250 : 0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [pickerOpen, repoSearch, repoPage]);

  const handleSubmit = async () => {
    if (isSubmitting) return;
    setError(null);
    setIsSubmitting(true);
    try {
      if (scope === 'product' && !productId) throw new Error('Open a Product to add shared information.');
      const inferred = sourceDetails(source);
      await createAsset({
        ...inferred,
        display_name: name.trim() || inferred.display_name,
        scope,
        ...(scope === 'product' ? { product_id: productId } : {}),
      });
      if (!active.current) return;
      onAssetAdded();
      onClose();
    } catch (err: unknown) {
      if (!active.current) return;
      if (err && typeof err === 'object' && 'message' in err) setError(String(err.message));
      else if (err && typeof err === 'object' && 'detail' in err && typeof err.detail === 'string') setError(err.detail);
      else if (err && typeof err === 'object' && 'detail' in err && err.detail && typeof err.detail === 'object' && 'message' in err.detail) setError(String(err.detail.message));
      else setError('Could not add this information. Please try again.');
    } finally {
      if (active.current) setIsSubmitting(false);
    }
  };

  if (showBulk) return <BulkUploadDialog isOpen scope={scope} productId={productId}
    onClose={() => setShowBulk(false)} onAssetsAdded={() => { onAssetAdded(); onClose(); }} />;

  return <Modal isOpen onClose={() => { if (!isSubmitting) onClose(); }} title="Add information" size="lg">
    <form onSubmit={(event) => { event.preventDefault(); void handleSubmit(); }} className="space-y-4">
      <p className="text-sm text-gray-600 dark:text-gray-400">
        {scope === 'product' ? 'Your Product team and platform admins can access information added here.' : 'Information added here stays in your personal Context.'}
      </p>
      <Input id="knowledge-source" label="Link" value={source} onChange={(event) => setSource(event.target.value)}
        placeholder="Paste a web link, GitHub URL, or s3:// URI" disabled={isSubmitting}
        helperText="Use an existing web page, repository, or document in an authorized S3 location." />
      <Input id="knowledge-name" label="Name (optional)" value={name} onChange={(event) => setName(event.target.value)} disabled={isSubmitting} />
      <Button type="button" variant="ghost" size="sm" aria-expanded={pickerOpen} aria-controls="knowledge-repo-picker"
        disabled={isSubmitting} onClick={() => setPickerOpen(!pickerOpen)}>Choose a connected repository</Button>
      {pickerOpen && <div id="knowledge-repo-picker" className="space-y-3">
        <Input id="knowledge-repo-search" label="Search repositories" value={repoSearch} disabled={isSubmitting}
          onChange={(event) => { setRepoSearch(event.target.value); setRepoPage(1); }} />
        {repoError && <p role="alert" className="text-sm text-red-700 dark:text-red-300">{repoError}</p>}
        <div className="max-h-60 overflow-y-auto rounded-md bg-gray-50 dark:bg-gray-900">
          {reposLoading && <p role="status" className="p-4 text-sm text-gray-500">Loading repositories…</p>}
          {!reposLoading && !repoError && repos.length === 0 && <p className="p-4 text-sm text-gray-500">No connected repositories found. You can paste a link above.</p>}
          {!reposLoading && !repoError && repos.map((repo) => <button key={repo.fullName} type="button" disabled={isSubmitting}
            className="w-full px-4 py-3 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800 focus-visible:ring-2 focus-visible:ring-primary-500"
            onClick={() => { setSource(repo.url); if (!name) setName(repo.fullName); setPickerOpen(false); }}>
            <span className="font-medium text-gray-900 dark:text-white">{repo.fullName}</span>
            {repo.private && <span className="ml-2 text-xs text-gray-500"> Private</span>}
          </button>)}
        </div>
        {hasMoreRepos && !reposLoading && !repoError && <Button type="button" variant="secondary" size="sm" onClick={() => setRepoPage(repoPage + 1)}>More repositories</Button>}
      </div>}
      <div><Button type="button" variant="ghost" size="sm" disabled={isSubmitting}
        onClick={() => setShowBulk(true)}>Import a list of links</Button></div>
      {error && <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-300">{error}</p>}
      <ModalFooter>
        <Button type="button" variant="secondary" onClick={onClose} disabled={isSubmitting}>Cancel</Button>
        <Button type="submit" disabled={isSubmitting}>{isSubmitting ? 'Adding…' : 'Add information'}</Button>
      </ModalFooter>
    </form>
  </Modal>;
}
