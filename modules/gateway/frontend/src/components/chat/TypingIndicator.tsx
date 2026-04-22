/**
 * TypingIndicator — animated dots + optional tool-use status line.
 *
 * Issue #97 Phase 1.
 */

import type { ToolUseInfo } from '@/types/chat';

interface TypingIndicatorProps {
  toolUse?: ToolUseInfo | null;
}

const TOOL_LABELS: Record<string, string> = {
  WebSearch: 'Searching the web...',
  ReadFile: 'Reading file...',
  WriteFile: 'Writing file...',
  RunCommand: 'Running command...',
  Search: 'Searching...',
  Grep: 'Searching codebase...',
  Glob: 'Finding files...',
};

export function TypingIndicator({ toolUse }: TypingIndicatorProps) {
  const toolLabel = toolUse
    ? TOOL_LABELS[toolUse.tool_name] || `Using ${toolUse.tool_name}...`
    : null;

  return (
    <div className="flex flex-col gap-1" aria-label="Agent is typing" role="status">
      {/* Animated dots */}
      <div className="flex items-center gap-1 px-4 py-2">
        <span className="w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500 animate-bounce [animation-delay:0ms]" />
        <span className="w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500 animate-bounce [animation-delay:150ms]" />
        <span className="w-2 h-2 rounded-full bg-gray-400 dark:bg-gray-500 animate-bounce [animation-delay:300ms]" />
      </div>

      {/* Tool use status */}
      {toolLabel && (
        <div className="flex items-center gap-2 px-4 text-xs text-gray-500 dark:text-gray-400">
          <span aria-hidden="true">&#128270;</span>
          <span>{toolLabel}</span>
        </div>
      )}
    </div>
  );
}
