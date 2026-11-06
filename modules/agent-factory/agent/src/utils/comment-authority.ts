import { createHash } from 'crypto';

/** GitHub comments are input; only a current repository writer may authorize work.
 * New executable comment consumers must use this decision (or the bound approval
 * verifier) before effects. Fenced context and reporting-only readers cannot
 * grant approval. Classify new readers in comment-consumers-7845.json.
 */
export interface CommentEnvelope {
  id: string;
  author: string;
  authorId: number;
  authorType: string;
  body: string;
  createdAt: string;
  updatedAt: string;
}

export interface CommentAuthorityContext {
  owner: string;
  repo: string;
  effectRepo?: { owner: string; repo: string };
  runId: string;
  tokenProvider: () => Promise<string>;
  cache?: CommentAuthorityCache;
  now?: () => number;
}

type DenialReason = 'machine_author' | 'invalid_author' | 'insufficient_permission' |
  'permission_unavailable' | 'stale_comment';
export type CommentAuthorityDecision =
  | { allowed: true; authorId: number; permission: string; cacheAgeMs?: number }
  | { allowed: false; reason: DenialReason; permission?: string; cacheAgeMs?: number };

type PermissionResult = { allowed: boolean; reason: 'insufficient_permission' | 'permission_unavailable' | 'invalid_author'; permission?: string; cacheAgeMs?: number };

async function lookupRepositoryPermission(
  owner: string, repo: string, login: string, token: string, authorId?: number,
): Promise<PermissionResult> {
  if (!owner || !repo || !token || !/^[a-z\d](?:[a-z\d-]*[a-z\d])?$/i.test(login)) {
    return { allowed: false, reason: 'invalid_author' };
  }
  try {
    // nosemgrep: tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf
    const response = await fetch(
      `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/collaborators/${encodeURIComponent(login)}/permission`,
      { redirect: 'error', headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json' }, signal: AbortSignal.timeout(10_000) },
    );
    if (!response.ok) return { allowed: false, reason: response.status === 404 ? 'insufficient_permission' : 'permission_unavailable' };
    const data = await response.json() as { permission?: unknown; user?: { id?: unknown } };
    if (!data || typeof data.permission !== 'string') return { allowed: false, reason: 'permission_unavailable' };
    if (authorId !== undefined && data.user?.id !== undefined && data.user.id !== authorId) {
      return { allowed: false, reason: 'invalid_author' };
    }
    return {
      allowed: ['admin', 'maintain', 'write'].includes(data.permission),
      reason: 'insufficient_permission', permission: data.permission,
    };
  } catch {
    return { allowed: false, reason: 'permission_unavailable' };
  }
}

export class CommentAuthorityCache {
  private entries = new Map<string, { result: PermissionResult; checkedAt: number; expiresAt: number }>();
  private pending = new Map<string, Promise<PermissionResult>>();
  private tokenGeneration = '';

  async check(
    owner: string, repo: string, login: string, authorId: number, token: string,
    now: number, fresh = false,
  ): Promise<PermissionResult> {
    const generation = createHash('sha256').update(token).digest('hex');
    if (this.tokenGeneration !== generation) {
      this.entries.clear();
      this.pending.clear();
      this.tokenGeneration = generation;
    }
    const key = JSON.stringify([owner, repo, authorId, login, generation]);
    const cached = this.entries.get(key);
    if (!fresh && cached && cached.expiresAt > now) {
      this.entries.delete(key);
      this.entries.set(key, cached);
      return { ...cached.result, cacheAgeMs: Math.max(0, now - cached.checkedAt) };
    }
    const existing = this.pending.get(key);
    if (!fresh && existing) return existing;
    const lookup = lookupRepositoryPermission(owner, repo, login, token, authorId);
    if (!fresh) this.pending.set(key, lookup);
    try {
      const result = await lookup;
      const ttl = result.allowed ? 60_000 : result.reason === 'permission_unavailable' ? 5_000 : 15_000;
      this.entries.delete(key);
      this.entries.set(key, { result, checkedAt: now, expiresAt: now + ttl });
      if (this.entries.size > 256) this.entries.delete(this.entries.keys().next().value!);
      return { ...result, cacheAgeMs: 0 };
    } finally {
      if (!fresh && this.pending.get(key) === lookup) this.pending.delete(key);
    }
  }
}

export async function authorizeComment(
  comment: CommentEnvelope, context: CommentAuthorityContext, fresh = false,
): Promise<CommentAuthorityDecision> {
  const now = context.now?.() ?? Date.now();
  const createdAt = Date.parse(comment?.createdAt ?? '');
  const updatedAt = Date.parse(comment?.updatedAt ?? '');
  if (!comment || !Number.isSafeInteger(comment.authorId) || comment.authorId <= 0 ||
      !comment.id || !context.runId || !context.owner || !context.repo ||
      !comment.author || !comment.authorType || !Number.isFinite(createdAt) ||
      !Number.isFinite(updatedAt)) {
    return { allowed: false, reason: 'invalid_author' };
  }
  if (createdAt > now + 60_000 || updatedAt > now + 60_000 || updatedAt > createdAt) {
    return { allowed: false, reason: 'stale_comment' };
  }
  if (comment.authorType !== 'User' || comment.author.includes('[bot]') ||
      comment.author === 'github-actions' || comment.author.toLowerCase().startsWith('aws-e-adp-agent-')) {
    return { allowed: false, reason: 'machine_author' };
  }
  try {
    const token = await context.tokenProvider();
    if (!token) return { allowed: false, reason: 'permission_unavailable' };
    const scopes = [{ owner: context.owner, repo: context.repo }];
    if (context.effectRepo && (context.effectRepo.owner !== context.owner || context.effectRepo.repo !== context.repo)) {
      scopes.push(context.effectRepo);
    }
    let permission = '';
    let cacheAgeMs: number | undefined;
    for (const scope of scopes) {
      const result = context.cache
        ? await context.cache.check(scope.owner, scope.repo, comment.author, comment.authorId, token, now, fresh)
        : await lookupRepositoryPermission(scope.owner, scope.repo, comment.author, token, comment.authorId);
      if (!result.allowed) return { allowed: false, reason: result.reason, permission: result.permission, cacheAgeMs: result.cacheAgeMs };
      permission = result.permission ?? '';
      cacheAgeMs = result.cacheAgeMs;
    }
    return { allowed: true, authorId: comment.authorId, permission, cacheAgeMs };
  } catch {
    return { allowed: false, reason: 'permission_unavailable' };
  }
}

/** Compatibility wrapper; use the same permission predicate as comment decisions. */
export async function hasRepositoryWritePermission(
  owner: string, repo: string, login: string, token: string,
): Promise<boolean> {
  return (await lookupRepositoryPermission(owner, repo, login, token)).allowed;
}

/** Match an entire command and bind it to the particular posted plan. */
export function parsePlanApproval(body: string, requestId: string): { approved: boolean; feedback: string } | null {
  const match = body.trim().match(/^\/(approve|reject)\s+(\S+)(?:[ \t]+([^\r\n]+))?$/i);
  if (!match || match[2] !== requestId) return null;
  if (match[1].toLowerCase() === 'approve') return match[3] ? null : { approved: true, feedback: '' };
  return { approved: false, feedback: (match[3] ?? '').trim() };
}
