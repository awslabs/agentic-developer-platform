/**
 * Personal Context Identity Headers — Issue #1289
 *
 * Provides trusted identity headers for Context MCP Server requests.
 * Headers are sourced exclusively from dispatch metadata (SQS message fields
 * set by the authenticated dispatcher), never from agent/LLM input.
 *
 * The Context MCP Server (modules/agent-context/personal_context/identity.py)
 * requires:
 *   - X-Owner-Sub: Cognito subject (UUID v4, lowercase)
 *   - X-Tenant-Id: Organization/tenant identifier
 *
 * Both headers must be present for personal-context operations. If either is
 * missing, the MCP server will fail-closed (403) — this is by design.
 *
 * SECURITY: These headers are injected by the worker harness from trusted
 * dispatch metadata. Agent tool calls cannot override them. The identity is
 * set at dispatch time by the gateway (which validated the Cognito JWT) and
 * flows through SQS as part of the message envelope.
 */

/**
 * Identity context extracted from trusted dispatch metadata.
 * Immutable once constructed — cannot be modified by agent code.
 */
export interface PersonalContextIdentity {
  /** Cognito sub (UUID) of the dispatching user. Empty for service accounts. */
  readonly ownerSub: string;
  /** Tenant/org ID from the dispatch context. */
  readonly tenantId: string;
}

/**
 * Headers to inject into Context MCP Server requests.
 */
export interface PersonalContextHeaders {
  readonly 'X-Owner-Sub': string;
  readonly 'X-Tenant-Id': string;
}

/**
 * Build personal-context identity from trusted dispatch metadata.
 *
 * @param taskPayload - Fields from the SQS task message (set by dispatcher)
 * @returns Identity object, or null if required fields are missing
 */
export function buildPersonalContextIdentity(taskPayload: {
  cognito_sub?: string;
  tenant_id?: string;
  user_id?: string;
}): PersonalContextIdentity | null {
  // cognito_sub is the explicit field; fall back to user_id for backward
  // compatibility (webchat path sets user_id = Cognito sub).
  const ownerSub = taskPayload.cognito_sub || taskPayload.user_id || '';
  const tenantId = taskPayload.tenant_id || '';

  if (!ownerSub || !tenantId) {
    return null;
  }

  return Object.freeze({ ownerSub, tenantId });
}

/**
 * Convert identity to HTTP headers for Context MCP Server requests.
 *
 * @param identity - Trusted identity from dispatch metadata
 * @returns Headers object, or null if identity is missing
 */
export function getPersonalContextHeaders(
  identity: PersonalContextIdentity | null,
): PersonalContextHeaders | null {
  if (!identity || !identity.ownerSub || !identity.tenantId) {
    return null;
  }

  return Object.freeze({
    'X-Owner-Sub': identity.ownerSub,
    'X-Tenant-Id': identity.tenantId,
  });
}

/**
 * Build environment variables that expose personal-context identity to
 * subprocesses (e.g., the Claude Code SDK subprocess that calls MCP servers).
 *
 * These env vars are the transport mechanism: the worker harness sets them
 * from trusted dispatch metadata, and the MCP client reads them when
 * constructing requests. Agent code running in the subprocess inherits
 * these but cannot modify the parent process's env.
 *
 * @param identity - Trusted identity from dispatch metadata
 * @returns Env var map to merge into subprocess env, or empty object
 */
export function getPersonalContextEnvVars(
  identity: PersonalContextIdentity | null,
): Record<string, string> {
  if (!identity || !identity.ownerSub || !identity.tenantId) {
    return {};
  }

  return {
    ADP_OWNER_SUB: identity.ownerSub,
    ADP_TENANT_ID: identity.tenantId,
  };
}
