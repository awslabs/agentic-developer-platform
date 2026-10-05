/**
 * TranscriptViewer — modal that fetches and renders a run transcript from S3.
 *
 * Issue #3069: Renders the markdown transcript client-side using the
 * already-installed react-markdown + remark-gfm + rehype-highlight stack.
 * No stored HTML — dynamic render only. HTML in the markdown stays escaped
 * (no rehype-raw) to prevent XSS from agent-generated content.
 *
 * Issue #3767: Exports TranscriptContent (no Modal wrapper) for inline
 * embedding inside InvocationDetail. The full TranscriptViewer (with Modal)
 * remains for standalone use from the activity table.
 */

import { RunRecordSummary } from '@/components/RunRecordSummary';
import { parseRunRecord } from '@/utils/runRecord';
import { Children, isValidElement, type ReactNode } from 'react';
import './transcript.css';
import { useQuery } from '@tanstack/react-query';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { Modal } from '@/components/ui';
import { getMyTranscript, getAdminTranscript } from '@/services/activity';

/** Read rendered inline text without discarding emphasis or code in headings. */
function textContent(children: ReactNode): string {
  return Children.toArray(children).map(child =>
    isValidElement<{ children?: ReactNode }>(child)
      ? textContent(child.props.children)
      : typeof child === 'string' || typeof child === 'number' ? String(child) : '',
  ).join('');
}

function SectionIcon({ children }: { children: ReactNode }) {
  const title = textContent(children).toLowerCase();
  const path = /tool|command|terminal/.test(title) ? 'm4 5 6 7-6 7m9 0h7'
    : /user|request|instruction/.test(title) ? 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a8 8 0 0 1 16 0v2'
    : /summary|result|outcome/.test(title) ? 'M8 3H4v18h16V3h-4M8 3v4h8V3H8Zm0 9h8m-8 4h5'
    : /assistant|update|walkthrough/.test(title) ? 'M21 11a9 9 0 0 1-9 9H3l2-5a9 9 0 1 1 16-4Z'
    : 'M4 4h16v16H4V4Zm4 4h8m-8 4h8m-8 4h5';
  return <span className="transcript-section-icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={path} /></svg></span>;
}

/** Shared presentation for historical Markdown; the original record stays intact. */
export function TranscriptMarkdown({ markdown }: { markdown: string }) {
  return (
    <article className="transcript-document" aria-label="Run transcript">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          h2: ({ children }) => <h2><SectionIcon>{children}</SectionIcon>{children}</h2>,
          h3: ({ children }) => <h3><SectionIcon>{children}</SectionIcon>{children}</h3>,
          table: ({ children }) => <div className="transcript-table-scroll" tabIndex={0} role="region" aria-label="Transcript table"><table>{children}</table></div>,
          pre: ({ children }) => <pre tabIndex={0} aria-label="Code or command output">{children}</pre>,
        }}
      >
        {markdown.replace(/^<!-- adp-run-record:v1 [A-Za-z0-9+/=]{1,1400000} -->\r?\n?/, '')}
      </ReactMarkdown>
    </article>
  );
}

// ---------------------------------------------------------------------------
// TranscriptContent — the inner content without Modal wrapper (Issue #3767)
// ---------------------------------------------------------------------------

export interface TranscriptContentProps {
  invocationId: string | null;
  /** Use admin endpoint (tenant-scoped). */
  isAdmin?: boolean;
  tenantId?: string;
}

/**
 * Renders transcript content (loading, error, markdown) without a Modal shell.
 * Used inline inside InvocationDetail to avoid nested modals.
 */
export function TranscriptContent({
  invocationId,
  isAdmin = false,
  tenantId,
}: TranscriptContentProps) {
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
    enabled: !!invocationId,
    staleTime: 5 * 60 * 1000, // Cache for 5 min (transcripts are immutable)
    retry: false,
  });

  if (!invocationId) return null;

  return (
    <div>
      <div className="run-identity mb-4 text-xs"><span>Invocation ID: <code>{invocationId}</code></span>
        {markdown && <button type="button" className="text-blue-600" onClick={() => {
          const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
          const link = document.createElement('a'); link.href = url; link.download = `run-${invocationId}.md`; link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }}>Download original transcript</button>}
      </div>
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
        <div className="run-archive-grid">
          <RunRecordSummary record={parseRunRecord(markdown, invocationId)} />
          <div className="min-w-0"><TranscriptMarkdown markdown={markdown} /></div>
        </div>
      )}

      {!isLoading && !error && !markdown && (
        <div className="text-center py-12">
          <p className="text-gray-500 dark:text-gray-400">
            Transcript is empty.
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// TranscriptViewer — full modal wrapper for standalone use
// ---------------------------------------------------------------------------

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
  if (!invocationId) return null;

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Run Transcript" size="workspace">
      <TranscriptContent
        invocationId={invocationId}
        isAdmin={isAdmin}
        tenantId={tenantId}
      />
    </Modal>
  );
}
