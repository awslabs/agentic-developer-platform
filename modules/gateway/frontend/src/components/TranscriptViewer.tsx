/**
 * TranscriptViewer — modal that fetches and renders a run transcript from S3.
 *
 * Issue #3069: Renders the markdown transcript client-side using the
 * already-installed react-markdown + remark-gfm + rehype-highlight stack.
 * No stored HTML — dynamic render only. HTML in the markdown stays escaped
 * (no rehype-raw) to prevent XSS from agent-generated content.
 */

import { useQuery } from '@tanstack/react-query';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Modal } from '@/components/ui';
import { getMyTranscript, getAdminTranscript } from '@/services/activity';

export interface TranscriptViewerProps {
  invocationId: string | null;
  isOpen: boolean;
  onClose: () => void;
  /** Use admin endpoint (tenant-scoped). */
  isAdmin?: boolean;
  tenantId?: string;
}

export function TranscriptViewer({
  invocationId,
  isOpen,
  onClose,
  isAdmin = false,
  tenantId,
}: TranscriptViewerProps) {
  const {
    data: markdown,
    isLoading,
    error,
  } = useQuery({
    queryKey: ['transcript', invocationId, isAdmin, tenantId],
    queryFn: () => {
      if (!invocationId) return Promise.resolve('');
      return isAdmin
        ? getAdminTranscript(invocationId, tenantId)
        : getMyTranscript(invocationId);
    },
    enabled: isOpen && !!invocationId,
    staleTime: 5 * 60 * 1000, // Cache for 5 min (transcripts are immutable)
    retry: false,
  });

  if (!invocationId) return null;

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Run Transcript" size="xl">
      <div className="max-h-[70vh] overflow-y-auto">
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
            <span className="ml-3 text-gray-500 dark:text-gray-400">Loading transcript...</span>
          </div>
        )}

        {error && (
          <div className="text-center py-12">
            <p className="text-gray-500 dark:text-gray-400">
              {error instanceof Error && error.message === 'Transcript not available'
                ? 'Transcript not available for this invocation.'
                : 'Failed to load transcript.'}
            </p>
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-2">
              {error instanceof Error ? error.message : 'Unknown error'}
            </p>
          </div>
        )}

        {!isLoading && !error && markdown && (
          <article className="prose prose-sm dark:prose-invert max-w-none px-1">
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
              {markdown}
            </ReactMarkdown>
          </article>
        )}

        {!isLoading && !error && !markdown && (
          <div className="text-center py-12">
            <p className="text-gray-500 dark:text-gray-400">
              Transcript is empty.
            </p>
          </div>
        )}
      </div>
    </Modal>
  );
}
