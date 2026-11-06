import { authorizeComment, type CommentAuthorityContext, type CommentEnvelope } from './comment-authority';
import { resolveSuperpowerTriggerComment } from './superpower-guidance';

export { resolveSuperpowerTriggerComment as resolvePmTriggerComment };

export type HealthCommand = { type: 'approve' | 'skip' | 'retry' | 'cleanup' | 'hold' | 'custom'; customInstructions?: string };

export function parseHealthCommand(body: string): HealthCommand | null {
  const simple = body.trim().match(/^\/(approve|skip|retry|cleanup|hold)$/i);
  if (simple) return { type: simple[1].toLowerCase() as HealthCommand['type'] };
  const custom = body.trim().match(/^\/custom[ \t]+([\s\S]*\S)$/i);
  return custom ? { type: 'custom', customInstructions: custom[1].trim() } : null;
}

export async function selectPmDecision<T>(
  comments: CommentEnvelope[], context: CommentAuthorityContext, since: number,
  parse: (body: string) => T | null, triggerCommentId?: string, afterCommentId?: string,
): Promise<{ decision: T; comment: CommentEnvelope } | null> {
  const candidates = comments.filter(comment =>
    comment.id === triggerCommentId || Date.parse(comment.createdAt) > since ||
    (afterCommentId && Date.parse(comment.createdAt) === since && /^\d+$/.test(comment.id) &&
      /^\d+$/.test(afterCommentId) && BigInt(comment.id) > BigInt(afterCommentId)),
  ).sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id));
  for (const comment of candidates) {
    const decision = parse(comment.body);
    if (decision && (await authorizeComment(comment, context, true)).allowed) return { decision, comment };
  }
  return null;
}

export function hasPmReassessmentEvidence(hasPersistedState: boolean, linkedChildIssues: number): boolean {
  return hasPersistedState || linkedChildIssues > 0;
}
