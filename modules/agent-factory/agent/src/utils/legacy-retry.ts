import { authorizeComment, type CommentAuthorityContext, type CommentEnvelope } from './comment-authority';

export function parseLegacyRetry(body: string): { guidance?: string } | null {
  const match = body.trim().match(/^\/retry(?:[ \t\r\n]+([\s\S]*\S))?$/i);
  if (!match) return null;
  const guidance = match[1]?.trim();
  return guidance ? { guidance } : {};
}

export async function selectLegacyRetry(
  comments: CommentEnvelope[], context: CommentAuthorityContext, triggerId: string,
  senderId: number, now = Date.now(),
): Promise<{ guidance?: string } | null> {
  if (!triggerId || !Number.isSafeInteger(senderId) || senderId <= 0) return null;
  const comment = comments.find(candidate => candidate.id === triggerId && candidate.authorId === senderId);
  if (!comment || Date.parse(comment.createdAt) < now - 5 * 60_000) return null;
  const retry = parseLegacyRetry(comment.body);
  return retry && (await authorizeComment(comment, context, true)).allowed ? retry : null;
}
