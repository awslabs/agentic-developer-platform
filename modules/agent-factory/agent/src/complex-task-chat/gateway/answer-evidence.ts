/**
 * Source coverage a turn reports for its answer (#8113).
 *
 * Mirrors the gateway's `AnswerEvidence` (src/agentauth/chat_answer_evidence.py) and the
 * frontend parser (hooks/useAgUiEvents.ts `parseAnswerEvidence`), bound for bound: it is
 * sent with the final model invocation (the delivery relay attaches it to the streamed
 * assistant message as the AG-UI `answer_evidence` event) and recorded on the turn result.
 */
import { z } from 'zod';

const instant = z.string().max(64).regex(/^\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:\d{2})$/);
export const coverageStatus = z.enum(['available', 'empty', 'denied', 'unavailable', 'partial']);
export const evidenceCoverage = z.object({
  source: z.string().min(1).max(100),
  status: coverageStatus,
  reason: z.string().min(1).max(160).optional(),
}).strict();
export const evidenceSource = z.object({
  label: z.string().min(1).max(100),
  url: z.string().min(1).max(2048).refine(value => value.startsWith('https://') || (value.startsWith('/') && !value.startsWith('//'))),
}).strict();
export const answerEvidenceSchema = z.object({
  status: z.enum(['ok', 'empty', 'denied', 'unavailable', 'partial']),
  from: instant.optional(),
  to: instant.optional(),
  timezone: z.string().min(1).max(64).optional(),
  observed_at: instant.optional(),
  coverage: z.array(evidenceCoverage).max(32).default([]),
  sources: z.array(evidenceSource).max(32).default([]),
  has_more: z.boolean().default(false),
}).strict();

export type AnswerEvidence = z.infer<typeof answerEvidenceSchema>;
export type EvidenceCoverage = z.infer<typeof evidenceCoverage>;
export type EvidenceSource = z.infer<typeof evidenceSource>;
