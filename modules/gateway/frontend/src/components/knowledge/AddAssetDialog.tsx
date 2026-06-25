/**
 * Add Asset Dialog — modal for adding repos, URLs, or docs.
 *
 * Issue #1794 (Story E of E10 #1736): repo picker + URL input + doc placeholder.
 * Repo picker powered by GET /api/agent-context/github/accessible-repos (PR #1907).
 */

import { useState, useCallback, useEffect, useRef } from 'react';
import { Modal, ModalFooter } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Tabs, TabsList, Tab, TabPanel } from '@/components/ui/Tabs';
import { getAccessibleRepos, createAsset } from '@/services/knowledge';
import type { AccessibleRepo } from '@/types';

export interface AddAssetDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onAssetAdded: () => void;
  scope: 'personal' | 'tenant';
}

export function AddAssetDialog({ isOpen, onClose, onAssetAdded, scope }: AddAssetDialogProps) {
  const [activeTab, setActiveTab] = useState('repo');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Repo picker state
  const [repos, setRepos] = useState<AccessibleRepo[]>([]);
  const [repoSearch, setRepoSearch] = useState('');
  const [reposLoading, setReposLoading] = useState(false);
  const [selectedRepo, setSelectedRepo] = useState<AccessibleRepo | null>(null);

  // URL input state
  const [urlInput, setUrlInput] = useState('');
  const [urlDisplayName, setUrlDisplayName] = useState('');

  // Doc input state (placeholder)
  const [docName, setDocName] = useState('');

  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Fetch repos when dialog opens or search changes
  const fetchRepos = useCallback(async (search?: string) => {
    setReposLoading(true);
    try {
      const response = await getAccessibleRepos({ search, page: 1, pageSize: 20 });
      setRepos(response.repos);
    } catch {
      setRepos([]);
    } finally {
      setReposLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen && activeTab === 'repo') {
      fetchRepos(repoSearch || undefined);
    }
  }, [isOpen, activeTab, fetchRepos, repoSearch]);

  // Debounced search for repos
  const handleRepoSearchChange = useCallback(
    (value: string) => {
      setRepoSearch(value);
      if (searchTimeoutRef.current) {
        clearTimeout(searchTimeoutRef.current);
      }
      searchTimeoutRef.current = setTimeout(() => {
        fetchRepos(value || undefined);
      }, 300);
    },
    [fetchRepos],
  );

  // Reset state on close
  useEffect(() => {
    if (!isOpen) {
      setActiveTab('repo');
      setError(null);
      setSelectedRepo(null);
      setRepoSearch('');
      setUrlInput('');
      setUrlDisplayName('');
      setDocName('');
      setIsSubmitting(false);
    }
  }, [isOpen]);

  const handleSubmit = async () => {
    setError(null);
    setIsSubmitting(true);

    try {
      if (activeTab === 'repo') {
        if (!selectedRepo) {
          setError('Please select a repository');
          setIsSubmitting(false);
          return;
        }
        await createAsset({
          asset_type: 'repo',
          source_ref: selectedRepo.url,
          display_name: selectedRepo.fullName,
          scope,
        });
      } else if (activeTab === 'url') {
        if (!urlInput.trim()) {
          setError('Please enter a URL');
          setIsSubmitting(false);
          return;
        }
        await createAsset({
          asset_type: 'url',
          source_ref: urlInput.trim(),
          display_name: urlDisplayName.trim() || undefined,
          scope,
        });
      } else if (activeTab === 'doc') {
        if (!docName.trim()) {
          setError('Please enter a document name');
          setIsSubmitting(false);
          return;
        }
        await createAsset({
          asset_type: 'doc',
          source_ref: `doc://${docName.trim()}`,
          display_name: docName.trim(),
          scope,
        });
      }

      onAssetAdded();
      onClose();
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'message' in err) {
        setError(String((err as { message: string }).message));
      } else if (err && typeof err === 'object' && 'detail' in err) {
        const detail = (err as { detail: unknown }).detail;
        if (typeof detail === 'string') {
          setError(detail);
        } else if (detail && typeof detail === 'object' && 'message' in detail) {
          setError(String((detail as { message: string }).message));
        } else {
          setError('Failed to add asset');
        }
      } else {
        setError('Failed to add asset');
      }
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Add Asset" size="lg">
      <Tabs defaultValue="repo" onChange={setActiveTab}>
        <TabsList>
          <Tab value="repo">Repository</Tab>
          <Tab value="url">URL</Tab>
          <Tab value="doc">Document</Tab>
        </TabsList>

        {/* Repo picker */}
        <TabPanel value="repo">
          <div className="space-y-3">
            <Input
              placeholder="Search repositories..."
              value={repoSearch}
              onChange={(e) => handleRepoSearchChange(e.target.value)}
            />
            <div className="max-h-60 overflow-y-auto border border-gray-200 dark:border-gray-700 rounded-md">
              {reposLoading && (
                <div className="p-4 text-center text-sm text-gray-500 dark:text-gray-400">
                  Loading repositories...
                </div>
              )}
              {!reposLoading && repos.length === 0 && (
                <div className="p-4 text-center text-sm text-gray-500 dark:text-gray-400">
                  No repositories found. Ensure a GitHub App is installed.
                </div>
              )}
              {!reposLoading &&
                repos.map((repo) => (
                  <button
                    key={repo.fullName}
                    type="button"
                    className={`w-full text-left px-4 py-2 text-sm border-b last:border-b-0 border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors ${
                      selectedRepo?.fullName === repo.fullName
                        ? 'bg-primary-50 dark:bg-primary-900/20 border-l-2 border-l-primary-500'
                        : ''
                    }`}
                    onClick={() => setSelectedRepo(repo)}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-gray-900 dark:text-white">
                        {repo.fullName}
                      </span>
                      {repo.private && (
                        <span className="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">
                          Private
                        </span>
                      )}
                    </div>
                  </button>
                ))}
            </div>
            {selectedRepo && (
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Selected: <span className="font-medium">{selectedRepo.fullName}</span>
              </p>
            )}
          </div>
        </TabPanel>

        {/* URL input */}
        <TabPanel value="url">
          <div className="space-y-3">
            <Input
              placeholder="https://example.com/docs/page"
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              label="URL"
            />
            <Input
              placeholder="Optional display name"
              value={urlDisplayName}
              onChange={(e) => setUrlDisplayName(e.target.value)}
              label="Display Name"
            />
          </div>
        </TabPanel>

        {/* Doc input (placeholder for future file upload) */}
        <TabPanel value="doc">
          <div className="space-y-3">
            <Input
              placeholder="Document name"
              value={docName}
              onChange={(e) => setDocName(e.target.value)}
              label="Document Name"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Document upload will be available in a future release. For now, enter a name to
              register a placeholder.
            </p>
          </div>
        </TabPanel>
      </Tabs>

      {error && (
        <div className="mt-3 rounded-md bg-red-50 dark:bg-red-900/20 p-3">
          <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
        </div>
      )}

      <ModalFooter>
        <Button variant="secondary" onClick={onClose} disabled={isSubmitting}>
          Cancel
        </Button>
        <Button onClick={handleSubmit} disabled={isSubmitting}>
          {isSubmitting ? 'Adding...' : 'Add Asset'}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
