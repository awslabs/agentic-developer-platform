/**
 * Door transport selection for protected and legacy workers.
 *
 * Protected runs use the loopback bridge. It rereads this run's credentials and
 * signs a fixed gateway route per request; the isolated gateway owns the Door
 * key and derives ACL identity from protected records. No static MCP headers
 * contain either a shared key or a rotating run credential.
 *
 * Issue #4073, finding #8. The Door derives every ACL decision from the identity
 * headers its caller supplies (X-GitHub-Login, X-GitHub-Teams, X-Owner-Sub,
 * X-Tenant-Id). Until #4073 nothing authenticated the caller, so any pod that
 * could reach the ClusterIP could assert an arbitrary identity and read another
 * tenant's indexed source, wikis and agent memory. The Door now requires this
 * header on every path except /health.
 *
 * There are three Door callers in this package — the native MCP
 * transport (knowledge-layer-config.ts), the experience save hook, and
 * recall-at-task-start. All select the protected bridge here. The shared-key
 * lookup below remains only for unprotected legacy callers.
 */

/**
 * Returns the Door authentication header, or an empty object when no key is
 * configured.
 *
 * Deliberately does NOT throw on a missing key. The Door verbs are an
 * enhancement rather than a hard dependency — KNOWLEDGE_LAYER_ENABLED defaults
 * off, and the save/recall hooks are best-effort — so an unset key must degrade
 * to "Door calls get 401 and the caller logs it", not "the agent crashes before
 * doing the work it was summoned for". The Door is the side that fails closed:
 * with no key configured it returns 503 and serves nothing.
 *
 * DOOR_API_KEY is preferred; GATEWAY_INTERNAL_API_KEY is accepted as a fallback
 * because it is the name the same secret already carries in the agent-context
 * ScaledJob (manifests/ingestion-scaledjob.yaml) and the value is identical.
 */
import { isProtectedKnowledgeRun, KNOWLEDGE_BRIDGE_URL } from './knowledgeBridge';

export function getDoorBaseUrl(legacy: string): string {
  return isProtectedKnowledgeRun() ? KNOWLEDGE_BRIDGE_URL : legacy;
}

export function getDoorHeaders(identity: Record<string, string> = {}): Record<string, string> {
  return isProtectedKnowledgeRun() ? {} : { ...getDoorAuthHeaders(), ...identity };
}

export function getDoorAuthHeaders(): Record<string, string> {
  if (isProtectedKnowledgeRun()) return {};
  const key = process.env.DOOR_API_KEY || process.env.GATEWAY_INTERNAL_API_KEY;
  return key ? { 'X-Internal-Api-Key': key } : {};
}
