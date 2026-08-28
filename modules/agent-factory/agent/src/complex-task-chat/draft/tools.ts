/**
 * update_draft MCP tool (#4208) — lets the intent-refinement persona keep the
 * user's live draft panel current as an intake conversation progresses.
 *
 * Follows the `publish_artifact` closure-factory pattern
 * (artifacts/s3-artifact-store.ts:235): the session id is captured in the
 * closure at turn construction time and the handler NEVER reads it from tool
 * input. There is deliberately no `session_id` argument in the input schema —
 * the model cannot name a session, so it cannot write into another user's
 * draft even if it is prompt-injected into trying.
 */
import { z } from 'zod';
import { AgentTool, AgentToolResult } from '../context/types';
import { DraftStore, IntentDraft } from './port';

export interface DraftToolsScope {
  /** Session whose draft this turn's tools write to. Closure-injected. */
  sessionId: string;
  /** Called after a successful write so the orchestrator can emit STATE_DELTA. */
  onUpdate?: (draft: IntentDraft) => void | Promise<void>;
}

/** Cap list fields so a runaway model cannot write an unbounded DDB row. */
const MAX_LIST_ITEMS = 20;
const MAX_FIELD_CHARS = 2000;

function clampText(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  return trimmed.slice(0, MAX_FIELD_CHARS);
}

function clampList(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const items = value
    .map(v => clampText(v))
    .filter((v): v is string => v !== undefined)
    .slice(0, MAX_LIST_ITEMS);
  return items.length > 0 ? items : undefined;
}

/**
 * Build the draft tools for one turn, bound to one session.
 *
 * @param store persistence for the draft
 * @param scope session + update callback, captured in the returned closures
 */
export function draftToolsForTurn(store: DraftStore, scope: DraftToolsScope): AgentTool[] {
  const { sessionId, onUpdate } = scope;

  return [
    {
      name: 'update_draft',
      description:
        'Update the live intent draft shown beside the conversation. Send the COMPLETE ' +
        'current draft every time — this replaces the draft rather than merging into it, ' +
        'so any field you omit is cleared. Call this as soon as you learn something that ' +
        'firms up a field; the user is watching the panel fill in. Leave fields out when ' +
        'you genuinely do not know them yet — do not invent content to look thorough.',
      inputSchema: {
        intent: z
          .string()
          .optional()
          .describe('One or two sentences: the outcome the user wants, in their words, sharpened.'),
        motivation: z
          .string()
          .optional()
          .describe('Why they want it — the problem it solves or the cost of not having it.'),
        outcomes: z
          .array(z.string())
          .optional()
          .describe('Concrete, observable results that mean this worked.'),
        constraints: z
          .array(z.string())
          .optional()
          .describe('Deadlines, systems that must be used or avoided, compliance, budget.'),
        open_questions: z
          .array(z.string())
          .optional()
          .describe('What you still need to know, or decisions the user has deferred.'),
      },
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        const draft: IntentDraft = {
          intent: clampText(input.intent),
          motivation: clampText(input.motivation),
          outcomes: clampList(input.outcomes),
          constraints: clampList(input.constraints),
          openQuestions: clampList(input.open_questions),
        };

        // sessionId comes from the closure, NOT from `input` — an argument
        // naming another session is simply not part of the schema and is
        // ignored here.
        const stored = await store.put(sessionId, draft);
        await onUpdate?.(stored);

        const filled = Object.entries(stored)
          .filter(([k, v]) => k !== 'updatedAt' && v !== undefined)
          .map(([k]) => k);

        return {
          content: [
            {
              type: 'text',
              text:
                filled.length > 0
                  ? `Draft updated (${filled.join(', ')}). The panel now shows this to the user.`
                  : 'Draft cleared — no fields were provided.',
            },
          ],
        };
      },
    },
  ];
}
