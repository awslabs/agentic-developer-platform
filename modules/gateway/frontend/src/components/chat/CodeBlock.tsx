/**
 * CodeBlock — syntax-highlighted code block with copy-to-clipboard button.
 *
 * Issue #97 Phase 1.
 */

import { useCallback, useState, type ReactNode } from 'react';

interface CodeBlockProps {
  className?: string;
  children?: ReactNode;
}

export function CodeBlock({ className, children }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  // Extract language from className produced by rehype-highlight (e.g. "hljs language-ts")
  const langMatch = className?.match(/language-(\w+)/);
  const language = langMatch?.[1] ?? '';

  const codeText = extractText(children);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(codeText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for insecure contexts
      const ta = document.createElement('textarea');
      ta.value = codeText;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [codeText]);

  return (
    <div className="relative group my-3 rounded-lg overflow-hidden border border-gray-200 dark:border-gray-700">
      {/* Header bar */}
      <div className="flex items-center justify-between bg-gray-100 dark:bg-gray-800 px-4 py-1.5 text-xs text-gray-500 dark:text-gray-400">
        <span>{language || 'code'}</span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 hover:text-gray-900 dark:hover:text-gray-200 transition-colors"
          aria-label={copied ? 'Copied' : 'Copy code'}
          data-testid="copy-code-button"
        >
          {copied ? (
            <>
              <CheckIcon /> Copied
            </>
          ) : (
            <>
              <CopyIcon /> Copy
            </>
          )}
        </button>
      </div>

      {/* Code content */}
      <pre className="overflow-x-auto p-4 text-sm bg-gray-50 dark:bg-gray-900">
        <code className={className}>{children}</code>
      </pre>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function extractText(node: ReactNode): string {
  if (typeof node === 'string') return node;
  if (typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(extractText).join('');
  if (node && typeof node === 'object' && 'props' in node) {
    return extractText((node as { props: { children?: ReactNode } }).props.children);
  }
  return '';
}

function CopyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}
