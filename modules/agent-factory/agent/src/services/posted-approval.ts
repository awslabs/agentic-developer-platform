import { createHash } from 'crypto';
import { authorizeComment, parsePlanApproval, type CommentAuthorityContext, type CommentEnvelope } from '../utils/comment-authority';

export interface PostedApproval {
  requestId: string;
  runId: string;
  owner: string;
  repo: string;
  issueNumber: number;
  planCommentId: string;
  planDigest: string;
  postedAt: string;
  expiresAt: number;
  state: 'pending' | 'consumed' | 'invalidated';
}

export type PostedApprovalOutcome =
  | { outcome: 'pending' }
  | { outcome: 'allowed-once'; approver: string; comment: string }
  | { outcome: 'rejected'; approver?: string; feedback: string; comment?: string }
  | { outcome: 'unavailable'; feedback: string };

export function bindPostedApproval(
  requestId: string, runId: string, owner: string, repo: string, issueNumber: number,
  planBody: string, receipt: { id: string | number; body: string; createdAt: string; updatedAt: string },
  timeoutMs: number,
): PostedApproval | null {
  const postedAt = Date.parse(receipt.createdAt);
  if (!requestId || !runId || !owner || !repo || !Number.isSafeInteger(issueNumber) || issueNumber <= 0 ||
      !receipt.id || !/^\d+$/.test(String(receipt.id)) || !planBody.trim() ||
      planBody !== receipt.body || !Number.isFinite(postedAt) ||
      receipt.updatedAt !== receipt.createdAt || !Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) return null;
  return {
    requestId, runId, owner, repo, issueNumber,
    planCommentId: String(receipt.id),
    planDigest: createHash('sha256').update(planBody).digest('hex'),
    postedAt: receipt.createdAt, expiresAt: Date.now() + timeoutMs, state: 'pending',
  };
}

export async function checkPostedApproval(
  request: PostedApproval, comments: CommentEnvelope[],
  getPlan: () => Promise<{ id: string | number; body: string; createdAt: string; updatedAt: string }>,
  context: CommentAuthorityContext,
  now = Date.now(),
): Promise<PostedApprovalOutcome> {
  if (request.state !== 'pending' || request.runId !== context.runId ||
      request.owner !== context.owner || request.repo !== context.repo ||
      now >= request.expiresAt) {
    request.state = 'invalidated';
    return { outcome: 'rejected', feedback: 'The approval request is no longer current.' };
  }
  const candidates = comments.filter(comment => parsePlanApproval(comment.body, request.requestId))
    .sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id));
  for (const comment of candidates) {
    const replyAt = Date.parse(comment.createdAt);
    const postedAt = Date.parse(request.postedAt);
    if (!Number.isFinite(replyAt) || replyAt < postedAt ||
        (replyAt === postedAt && (!/^\d+$/.test(request.planCommentId) ||
          !/^\d+$/.test(comment.id) || BigInt(comment.id) <= BigInt(request.planCommentId)))) continue;
    let plan;
    try { plan = await getPlan(); } catch {
      request.state = 'invalidated';
      return { outcome: 'unavailable', feedback: 'The posted plan could not be verified.' };
    }
    if (String(plan.id) !== request.planCommentId || plan.createdAt !== request.postedAt ||
        plan.updatedAt !== plan.createdAt ||
        createHash('sha256').update(plan.body).digest('hex') !== request.planDigest) {
      request.state = 'invalidated';
      return { outcome: 'rejected', feedback: 'The posted plan changed; publish a new request.' };
    }
    const authority = await authorizeComment(comment, context, true);
    if (!authority.allowed) {
      if (authority.reason === 'permission_unavailable') {
        request.state = 'invalidated';
        return { outcome: 'unavailable', feedback: 'Approval permission could not be verified.' };
      }
      continue;
    }
    const response = parsePlanApproval(comment.body, request.requestId)!;
    request.state = 'consumed';
    return response.approved
      ? { outcome: 'allowed-once', approver: comment.author, comment: comment.body }
      : { outcome: 'rejected', approver: comment.author, feedback: response.feedback, comment: comment.body };
  }
  return { outcome: 'pending' };
}

export async function waitForPostedApproval(
  request: PostedApproval,
  fetchComments: () => Promise<CommentEnvelope[]>,
  fetchPlan: () => Promise<{ id: string | number; body: string; createdAt: string; updatedAt: string }>,
  context: CommentAuthorityContext,
  pollingIntervalMs: number,
  maxPolls: number,
  maxErrors: number,
): Promise<Exclude<PostedApprovalOutcome, { outcome: 'pending' }>> {
  let failures = 0;
  for (let attempt = 0; attempt < maxPolls && Date.now() < request.expiresAt; attempt++) {
    if (attempt) await new Promise(resolve => setTimeout(resolve, pollingIntervalMs));
    if (Date.now() >= request.expiresAt) break;
    try {
      const result = await checkPostedApproval(request, await fetchComments(), fetchPlan, context);
      if (result.outcome !== 'pending') return result;
      failures = 0;
    } catch {
      if (++failures >= maxErrors) {
        request.state = 'invalidated';
        return { outcome: 'unavailable', feedback: 'Approval comments could not be read.' };
      }
    }
  }
  request.state = 'invalidated';
  return { outcome: 'rejected', feedback: 'No authorized approval received before the wait expired.' };
}


function approvalComment(data: Record<string, unknown>): CommentEnvelope {
  const user = data.user as { login?: string; id?: number; type?: string } | undefined;
  return {
    id: String(data.id ?? ''), body: typeof data.body === 'string' ? data.body : '',
    author: user?.login ?? '', authorId: user?.id ?? 0, authorType: user?.type ?? '',
    createdAt: String(data.created_at ?? ''), updatedAt: String(data.updated_at ?? ''),
  };
}

function approvalUrl(owner: string, repo: string, issueNumber: number, commentId?: string): string {
  if (!owner || !repo || !Number.isSafeInteger(issueNumber) || issueNumber <= 0 ||
      (commentId !== undefined && !/^\d+$/.test(commentId))) throw new Error('Invalid approval scope');
  return `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/issues/${commentId ? `comments/${commentId}` : `${issueNumber}/comments`}`;
}

export async function postApprovalPlan(
  owner: string, repo: string, issueNumber: number, body: string, token: string,
): Promise<{ id: string; body: string; createdAt: string; updatedAt: string }> {
  if (!token) throw new Error('Approval token unavailable');
  const response = await fetch(approvalUrl(owner, repo, issueNumber), {
    method: 'POST', redirect: 'error', headers: { Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json', 'Content-Type': 'application/json' },
    body: JSON.stringify({ body }), signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error('Approval plan could not be posted');
  const data = await response.json() as Record<string, unknown>;
  return { id: String(data.id ?? ''), body: String(data.body ?? ''),
    createdAt: String(data.created_at ?? ''), updatedAt: String(data.updated_at ?? '') };
}

export async function fetchApprovalPlan(
  owner: string, repo: string, issueNumber: number, commentId: string, token: string,
): Promise<{ id: string; body: string; createdAt: string; updatedAt: string }> {
  if (!token) throw new Error('Approval token unavailable');
  const response = await fetch(approvalUrl(owner, repo, issueNumber, commentId), {
    redirect: 'error', headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json' },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error('Approval plan unavailable');
  const data = await response.json() as Record<string, unknown>;
  return { id: String(data.id ?? ''), body: String(data.body ?? ''),
    createdAt: String(data.created_at ?? ''), updatedAt: String(data.updated_at ?? '') };
}

export async function fetchApprovalComments(
  owner: string, repo: string, issueNumber: number, token: string,
): Promise<CommentEnvelope[]> {
  if (!token) throw new Error('Approval token unavailable');
  const comments: CommentEnvelope[] = [];
  for (let page = 1; page <= 100; page++) {
    const response = await fetch(`${approvalUrl(owner, repo, issueNumber)}?per_page=100&page=${page}`, {
      redirect: 'error', headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json' },
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) throw new Error('Approval comments unavailable');
    const batch = await response.json() as unknown;
    if (!Array.isArray(batch)) throw new Error('Invalid approval comments');
    comments.push(...batch.map(approvalComment));
    if (batch.length < 100) return comments;
  }
  throw new Error('Approval comment page budget exhausted');
}
