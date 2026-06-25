/**
 * Bulk Upload Dialog — file dropzone, preview table, commit flow.
 *
 * Issue #1795 (Story F of E10 #1736).
 * Uploads a plain-text file (one asset per line) to the bulk preview endpoint,
 * renders valid/rejected/duplicates/quota, then commits on user confirmation.
 */

import { useState, useCallback, useEffect, useRef } from 'react';
import { Modal, ModalFooter } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { bulkPreview, bulkCommit } from '@/services/knowledge';
import type { BulkPreviewResponse, BulkCommitItem } from '@/types';

export interface BulkUploadDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onAssetsAdded: () => void;
  scope: 'personal' | 'tenant';
}

type DialogPhase = 'upload' | 'preview' | 'committing' | 'done';

export function BulkUploadDialog({
  isOpen,
  onClose,
  onAssetsAdded,
  scope,
}: BulkUploadDialogProps) {
  const [phase, setPhase] = useState<DialogPhase>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<BulkPreviewResponse | null>(null);
  const [commitResult, setCommitResult] = useState<{ created: number; skipped: number } | null>(
    null,
  );

  const dragCounter = useRef(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Reset state when dialog closes
  useEffect(() => {
    if (!isOpen) {
      setPhase('upload');
      setFile(null);
      setIsDragging(false);
      setIsLoading(false);
      setError(null);
      setPreview(null);
      setCommitResult(null);
      dragCounter.current = 0;
    }
  }, [isOpen]);

  // --- Drag-and-drop handlers ---
  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current += 1;
    if (dragCounter.current === 1) {
      setIsDragging(true);
    }
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current -= 1;
    if (dragCounter.current === 0) {
      setIsDragging(false);
    }
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current = 0;
    setIsDragging(false);

    const droppedFile = e.dataTransfer.files[0];
    if (droppedFile) {
      setFile(droppedFile);
      setError(null);
    }
  }, []);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (selectedFile) {
      setFile(selectedFile);
      setError(null);
    }
  }, []);

  // --- Preview action ---
  const handlePreview = async () => {
    if (!file) return;
    setIsLoading(true);
    setError(null);

    try {
      const result = await bulkPreview(file, scope);
      setPreview(result);
      setPhase('preview');
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'detail' in err) {
        const detail = (err as { detail: unknown }).detail;
        setError(typeof detail === 'string' ? detail : 'Failed to preview file');
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError('Failed to preview file');
      }
    } finally {
      setIsLoading(false);
    }
  };

  // --- Commit action ---
  const handleCommit = async () => {
    if (!preview || preview.valid.length === 0) return;
    setPhase('committing');
    setError(null);

    const items: BulkCommitItem[] = preview.valid.map((v) => ({
      source_ref: v.source_ref,
      asset_type: v.asset_type,
      display_name: v.display_name,
      tags: v.tags,
    }));

    try {
      const result = await bulkCommit({ items, scope });
      setCommitResult({ created: result.created, skipped: result.skipped_duplicates });
      setPhase('done');
      onAssetsAdded();
    } catch (err: unknown) {
      setPhase('preview'); // revert to preview so user can retry
      if (err && typeof err === 'object' && 'detail' in err) {
        const detail = (err as { detail: unknown }).detail;
        setError(typeof detail === 'string' ? detail : 'Failed to commit assets');
      } else if (err instanceof Error) {
        setError(err.message);
      } else {
        setError('Failed to commit assets');
      }
    }
  };

  // --- Go back to upload phase ---
  const handleBack = () => {
    setPhase('upload');
    setFile(null);
    setPreview(null);
    setError(null);
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Bulk Upload" size="xl">
      {/* Upload phase */}
      {phase === 'upload' && (
        <div className="space-y-4">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Upload a plain-text file with one asset per line. Supported formats:
          </p>
          <ul className="text-xs text-gray-500 dark:text-gray-400 list-disc list-inside space-y-0.5">
            <li>
              <code>source_ref</code> (URL or S3 path)
            </li>
            <li>
              <code>source_ref | display_name</code>
            </li>
            <li>
              <code>source_ref | display_name | key:value, key:value</code>
            </li>
          </ul>

          {/* Dropzone */}
          <div
            role="button"
            tabIndex={0}
            className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors ${
              isDragging
                ? 'border-primary-500 bg-primary-50 dark:bg-primary-900/20'
                : 'border-gray-300 dark:border-gray-600 hover:border-gray-400 dark:hover:border-gray-500'
            }`}
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') fileInputRef.current?.click();
            }}
          >
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              accept=".txt,.csv,.tsv"
              onChange={handleFileSelect}
              data-testid="bulk-file-input"
            />
            {file ? (
              <div>
                <p className="text-sm font-medium text-gray-900 dark:text-white">{file.name}</p>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  {(file.size / 1024).toFixed(1)} KB
                </p>
              </div>
            ) : (
              <div>
                <p className="text-sm text-gray-600 dark:text-gray-400">
                  Drop a file here or click to select
                </p>
                <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                  .txt, .csv, or .tsv (max 1 MB, 500 lines)
                </p>
              </div>
            )}
          </div>

          {error && (
            <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-3">
              <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
            </div>
          )}
        </div>
      )}

      {/* Preview phase */}
      {phase === 'preview' && preview && (
        <div className="space-y-4">
          {/* Summary badges */}
          <div className="flex flex-wrap gap-2">
            <Badge variant="success">{preview.valid.length} valid</Badge>
            {preview.rejected.length > 0 && (
              <Badge variant="danger">{preview.rejected.length} rejected</Badge>
            )}
            {preview.duplicates.length > 0 && (
              <Badge variant="warning">{preview.duplicates.length} duplicates</Badge>
            )}
            <Badge variant="default">
              {preview.total_lines} lines ({preview.skipped_comments} comments)
            </Badge>
          </div>

          {/* Quota info */}
          {!preview.quota_ok && (
            <div className="rounded-md bg-yellow-50 dark:bg-yellow-900/20 p-3">
              <p className="text-sm text-yellow-700 dark:text-yellow-300">
                Quota exceeded — some assets may not be created.
              </p>
            </div>
          )}
          {preview.quota_ok && Object.keys(preview.quota_after).length > 0 && (
            <div className="text-xs text-gray-500 dark:text-gray-400 flex gap-3">
              {Object.entries(preview.quota_after).map(([type, q]) => (
                <span key={type}>
                  {type}: {q.used}/{q.limit}
                </span>
              ))}
            </div>
          )}

          {/* Valid items table */}
          {preview.valid.length > 0 && (
            <div>
              <h4 className="text-sm font-medium text-gray-900 dark:text-white mb-2">
                Valid ({preview.valid.length})
              </h4>
              <div className="max-h-40 overflow-y-auto border border-gray-200 dark:border-gray-700 rounded-md">
                <table className="w-full text-xs">
                  <thead className="bg-gray-50 dark:bg-gray-800 sticky top-0">
                    <tr>
                      <th className="px-3 py-1.5 text-left text-gray-500 dark:text-gray-400">
                        Line
                      </th>
                      <th className="px-3 py-1.5 text-left text-gray-500 dark:text-gray-400">
                        Type
                      </th>
                      <th className="px-3 py-1.5 text-left text-gray-500 dark:text-gray-400">
                        Source
                      </th>
                      <th className="px-3 py-1.5 text-left text-gray-500 dark:text-gray-400">
                        Name
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.valid.map((item) => (
                      <tr
                        key={item.line}
                        className="border-t border-gray-100 dark:border-gray-700"
                      >
                        <td className="px-3 py-1.5 text-gray-500">{item.line}</td>
                        <td className="px-3 py-1.5 text-gray-700 dark:text-gray-300">
                          {item.asset_type}
                        </td>
                        <td className="px-3 py-1.5 text-gray-700 dark:text-gray-300 truncate max-w-[200px]">
                          {item.source_ref}
                        </td>
                        <td className="px-3 py-1.5 text-gray-700 dark:text-gray-300">
                          {item.display_name || '-'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Rejected items */}
          {preview.rejected.length > 0 && (
            <div>
              <h4 className="text-sm font-medium text-red-700 dark:text-red-300 mb-2">
                Rejected ({preview.rejected.length})
              </h4>
              <div className="max-h-32 overflow-y-auto border border-red-200 dark:border-red-700 rounded-md">
                <table className="w-full text-xs">
                  <thead className="bg-red-50 dark:bg-red-900/20 sticky top-0">
                    <tr>
                      <th className="px-3 py-1.5 text-left text-red-500">Line</th>
                      <th className="px-3 py-1.5 text-left text-red-500">Source</th>
                      <th className="px-3 py-1.5 text-left text-red-500">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rejected.map((item) => (
                      <tr key={item.line} className="border-t border-red-100 dark:border-red-800">
                        <td className="px-3 py-1.5 text-gray-500">{item.line}</td>
                        <td className="px-3 py-1.5 text-gray-700 dark:text-gray-300 truncate max-w-[180px]">
                          {item.source_ref}
                        </td>
                        <td className="px-3 py-1.5 text-red-600 dark:text-red-400">
                          {item.reason}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Duplicates */}
          {preview.duplicates.length > 0 && (
            <div>
              <h4 className="text-sm font-medium text-yellow-700 dark:text-yellow-300 mb-2">
                Duplicates ({preview.duplicates.length})
              </h4>
              <div className="max-h-32 overflow-y-auto border border-yellow-200 dark:border-yellow-700 rounded-md">
                <table className="w-full text-xs">
                  <thead className="bg-yellow-50 dark:bg-yellow-900/20 sticky top-0">
                    <tr>
                      <th className="px-3 py-1.5 text-left text-yellow-600">Line</th>
                      <th className="px-3 py-1.5 text-left text-yellow-600">Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.duplicates.map((item) => (
                      <tr
                        key={item.line}
                        className="border-t border-yellow-100 dark:border-yellow-800"
                      >
                        <td className="px-3 py-1.5 text-gray-500">{item.line}</td>
                        <td className="px-3 py-1.5 text-gray-700 dark:text-gray-300 truncate max-w-[250px]">
                          {item.source_ref}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {error && (
            <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-3">
              <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
            </div>
          )}
        </div>
      )}

      {/* Committing phase */}
      {phase === 'committing' && (
        <div className="flex flex-col items-center justify-center py-8">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600 mb-4" />
          <p className="text-sm text-gray-600 dark:text-gray-400">Committing assets...</p>
        </div>
      )}

      {/* Done phase */}
      {phase === 'done' && commitResult && (
        <div className="space-y-3 py-4">
          <div className="rounded-md bg-green-50 dark:bg-green-900/20 p-4 text-center">
            <p className="text-sm font-medium text-green-800 dark:text-green-200">
              {commitResult.created} asset{commitResult.created !== 1 ? 's' : ''} queued for
              indexing.
            </p>
            {commitResult.skipped > 0 && (
              <p className="text-xs text-green-600 dark:text-green-400 mt-1">
                {commitResult.skipped} duplicate{commitResult.skipped !== 1 ? 's' : ''} skipped.
              </p>
            )}
          </div>
        </div>
      )}

      {/* Footer buttons */}
      <ModalFooter>
        {phase === 'upload' && (
          <>
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button onClick={handlePreview} disabled={!file || isLoading}>
              {isLoading ? 'Uploading...' : 'Preview'}
            </Button>
          </>
        )}
        {phase === 'preview' && (
          <>
            <Button variant="secondary" onClick={handleBack}>
              Back
            </Button>
            <Button onClick={handleCommit} disabled={!preview || preview.valid.length === 0}>
              Commit {preview?.valid.length ?? 0} Asset{(preview?.valid.length ?? 0) !== 1 ? 's' : ''}
            </Button>
          </>
        )}
        {phase === 'committing' && (
          <Button variant="secondary" disabled>
            Committing...
          </Button>
        )}
        {phase === 'done' && (
          <Button onClick={onClose}>Done</Button>
        )}
      </ModalFooter>
    </Modal>
  );
}
