/**
 * ToolCallRow — collapsible tool call display for AG-UI TOOL_CALL events.
 *
 * Issue #97 Phase 2: Renders tool calls as collapsible rows below the
 * assistant message bubble. Shows tool name, status (running/complete),
 * and args when expanded.
 */

import { useState } from 'react';
import type { ToolCallInfo } from '@/types/ag-ui-events';

// Tool name → emoji mapping (matches renderToolUseProgress on server side)
const TOOL_ICONS: Record<string, string> = {
  WebSearch: '🔍',
  WebFetch: '🌐',
  Bash: '💻',
  Read: '📖',
  Write: '✏️',
  Edit: '✏️',
  Glob: '📂',
  Grep: '🔎',
  Skill: '🎯',
};

function getToolIcon(name: string): string {
  return TOOL_ICONS[name] ?? '🛠️';
}

function prettifyToolName(name: string): string {
  // Strip MCP prefix if present
  return name.replace(/^mcp__chat-agent-tools__/, '');
}

interface ToolCallRowProps {
  toolCall: ToolCallInfo;
}

export function ToolCallRow({ toolCall }: ToolCallRowProps) {
  const [expanded, setExpanded] = useState(false);
  const icon = getToolIcon(toolCall.toolCallName);
  const name = prettifyToolName(toolCall.toolCallName);
  const isRunning = toolCall.status === 'running';

  return (
    <div
      className="my-1 rounded-md border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 text-sm"
      role="group"
      aria-label={`Tool call: ${name}`}
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-gray-100 dark:hover:bg-gray-700/50 rounded-md transition-colors"
        onClick={() => setExpanded(!expanded)}
        aria-expanded={expanded}
      >
        <span className="shrink-0">{icon}</span>
        <span className="font-medium text-gray-700 dark:text-gray-300">{name}</span>
        {isRunning && (
          <span className="ml-auto flex items-center gap-1 text-xs text-blue-500">
            <span className="inline-block h-2 w-2 rounded-full bg-blue-500 animate-pulse" />
            Running
          </span>
        )}
        {!isRunning && (
          <span className="ml-auto text-xs text-green-600 dark:text-green-400">✓ Done</span>
        )}
        <svg
          className={`h-4 w-4 text-gray-400 transition-transform ${expanded ? 'rotate-90' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
      </button>
      {expanded && toolCall.args && (
        <div className="px-3 pb-2 pt-0">
          <pre className="text-xs text-gray-500 dark:text-gray-400 whitespace-pre-wrap break-all font-mono bg-gray-100 dark:bg-gray-900 p-2 rounded">
            {toolCall.args}
          </pre>
        </div>
      )}
    </div>
  );
}
