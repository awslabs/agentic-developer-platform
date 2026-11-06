import { productPath } from './products';

export type ConnectionService = 'github' | 'aws' | 'jira' | 'slack' | 'sonarqube' | 'jenkins' | 'argocd' | 'gitlab' | 'pagerduty' | 'linear' | 'slack_profile';
export type ProductConnectionContext = { productId: string; projectId: string };
type Actor = { id?: string; orgId?: string } | null;
const returnKey = 'adp_product_connection_return';
const lifetime = 30 * 60 * 1000;

export function isConnectionService(value: unknown): value is ConnectionService {
  return typeof value === 'string' && ['github', 'aws', 'jira', 'slack', 'sonarqube', 'jenkins', 'argocd', 'gitlab', 'pagerduty', 'linear', 'slack_profile'].includes(value);
}

/** Navigation only. Provider callbacks and connection APIs still verify authority. */
export function rememberProductConnection(context: ProductConnectionContext | undefined, service: 'github' | 'slack', actor: Actor) {
  sessionStorage.removeItem(returnKey);
  if (!context || !actor?.id || !actor.orgId) return;
  sessionStorage.setItem(returnKey, JSON.stringify({ ...context, service, userId: actor.id, orgId: actor.orgId, expiresAt: Date.now() + lifetime }));
}

export function takeProductConnectionReturn(actor: Actor, service?: string): string | null {
  const saved = sessionStorage.getItem(returnKey);
  if (!saved) return null;
  sessionStorage.removeItem(returnKey);
  try {
    const value = JSON.parse(saved);
    if (!actor?.id || !actor.orgId || value.userId !== actor.id || value.orgId !== actor.orgId
      || !['github', 'slack'].includes(value.service) || (service && value.service !== service)
      || !Number.isFinite(value.expiresAt) || value.expiresAt <= Date.now() || value.expiresAt > Date.now() + lifetime
      || typeof value.productId !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(value.productId)
      || typeof value.projectId !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(value.projectId)) return null;
    return `${productPath(value.productId, value.projectId)}?connection_setup=${value.service}`;
  } catch { return null; }
}
