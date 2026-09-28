/**
 * Per-tool input sanitizers for AG-UI event sanitization (#137).
 *
 * A tool may declare `inputSummarySanitizer` to strip sensitive fields from the
 * args echoed into TOOL_CALL_ARGS events. Extracted from the orchestrator in
 * #4208 so the "every tool is considered" property is directly testable —
 * the loop previously ran over the vault tool list only, so a sanitizer declared
 * by any other tool was silently ignored and its args went out unsanitized.
 */
import { AgentTool } from './context/types';

export type InputSanitizer = (input: Record<string, unknown>) => Record<string, unknown>;

interface MaybeSanitizable {
  inputSummarySanitizer?: InputSanitizer;
}

/**
 * Build the tool-name → sanitizer map from the COMPLETE per-turn tool list.
 * Pass every tool handed to the model, not a subset.
 */
export function buildToolSanitizers(tools: readonly AgentTool[]): Map<string, InputSanitizer> {
  const sanitizers = new Map<string, InputSanitizer>();
  for (const tool of tools) {
    const sanitizer = (tool as MaybeSanitizable).inputSummarySanitizer;
    if (sanitizer) {
      sanitizers.set(tool.name, sanitizer);
    }
  }
  return sanitizers;
}
