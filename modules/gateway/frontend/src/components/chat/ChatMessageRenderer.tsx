/**
 * ChatMessageRenderer — renders a single chat message with markdown support.
 *
 * Issue #97 Phase 1: Uses react-markdown + rehype-highlight for syntax
 * highlighting and remark-gfm for GitHub-flavored markdown (tables, etc.).
 */

import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import remarkGfm from 'remark-gfm';
import { CodeBlock } from './CodeBlock';
import { TypingIndicator } from './TypingIndicator';
import type { ChatMessage } from '@/types/chat';
import type { ComponentPropsWithoutRef } from 'react';

interface ChatMessageRendererProps {
  message: ChatMessage;
}

export function ChatMessageRenderer({ message }: ChatMessageRendererProps) {
  const isUser = message.role === 'user';
  const isSystem = message.role === 'system';
  const isError = message.status === 'error';
  const isStreaming = message.status === 'streaming';

  if (isSystem) {
    return (
      <div className="flex justify-center my-2" data-testid="system-message">
        <span className="text-xs text-gray-400 dark:text-gray-500 bg-gray-100 dark:bg-gray-800 rounded-full px-3 py-1">
          {message.content}
        </span>
      </div>
    );
  }

  return (
    <div
      className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-4`}
      data-testid={`message-${message.role}`}
    >
      <div
        className={`
          max-w-[80%] rounded-2xl px-4 py-3
          ${
            isUser
              ? 'bg-primary-600 text-white rounded-br-md'
              : isError
                ? 'bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-200 border border-red-200 dark:border-red-800 rounded-bl-md'
                : 'bg-white dark:bg-gray-800 text-gray-900 dark:text-white border border-gray-200 dark:border-gray-700 rounded-bl-md'
          }
        `}
      >
        {/* Message content */}
        {message.content ? (
          <div
            className={`prose prose-sm max-w-none ${
              isUser
                ? 'prose-invert'
                : 'dark:prose-invert'
            }`}
            data-testid="message-content"
          >
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeHighlight]}
              components={{
                // Custom code block rendering with copy button
                pre({ children }) {
                  return <>{children}</>;
                },
                code({ className, children, ...props }: ComponentPropsWithoutRef<'code'> & { inline?: boolean }) {
                  // If wrapped in pre → block code
                  const isInline = !className;
                  if (isInline) {
                    return (
                      <code
                        className="bg-gray-100 dark:bg-gray-700 rounded px-1.5 py-0.5 text-sm font-mono"
                        {...props}
                      >
                        {children}
                      </code>
                    );
                  }
                  return <CodeBlock className={className}>{children}</CodeBlock>;
                },
                // Style tables
                table({ children }) {
                  return (
                    <div className="overflow-x-auto my-2">
                      <table className="min-w-full text-sm">{children}</table>
                    </div>
                  );
                },
                // External links open in new tab
                a({ href, children, ...props }) {
                  return (
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-primary-600 dark:text-primary-400 underline"
                      {...props}
                    >
                      {children}
                    </a>
                  );
                },
              }}
            >
              {message.content}
            </ReactMarkdown>
          </div>
        ) : null}

        {/* Streaming indicator */}
        {isStreaming && !message.content && (
          <TypingIndicator toolUse={message.toolUse} />
        )}

        {/* Tool use indicator below content when streaming */}
        {isStreaming && message.content && message.toolUse && (
          <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-700">
            <TypingIndicator toolUse={message.toolUse} />
          </div>
        )}

        {/* Error reason */}
        {isError && message.errorReason && (
          <p className="text-xs mt-2 opacity-75" data-testid="error-reason">
            Error: {message.errorReason}
          </p>
        )}

        {/* Timestamp */}
        <div
          className={`text-xs mt-1 ${
            isUser ? 'text-primary-200' : 'text-gray-400 dark:text-gray-500'
          }`}
        >
          {new Date(message.timestamp).toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit',
          })}
        </div>
      </div>
    </div>
  );
}
