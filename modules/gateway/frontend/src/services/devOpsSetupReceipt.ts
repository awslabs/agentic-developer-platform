import { sameConsentScope } from './l5OwnerConsent';
import type { BindingRequest, DevOpsConfiguration, SecurityBindingOperation } from './l5Services';

export interface DevOpsSetupReceipt {
  key: string;
  ownerKey: string;
  configurationKey: string;
  request: BindingRequest;
  state: 'pending' | 'confirmed' | 'rejected';
  operation?: SecurityBindingOperation;
}
export const devOpsSetupStorageKey = (configuration: DevOpsConfiguration, productId: string, projectId: string) => {
  const scope = configuration.scope;
  return scope.product_id === productId && scope.project_id === projectId && scope.owner_id?.trim()
    ? `adp:devops-setup:${JSON.stringify([scope.tenant_id, productId, projectId, scope.owner_id, configuration.configuration_id])}` : null;
};
export function setupOperationMatches(operation: SecurityBindingOperation, original: DevOpsSetupReceipt) {
  return operation?.action === original.request.action && typeof operation.operation_id === 'string' && operation.operation_id.length > 0
    && Number.isInteger(operation.revision) && operation.revision > 0
    && ['accepted', 'running', 'waiting', 'succeeded', 'blocked', 'failed', 'outcome_unknown', 'cancelled', 'abandoned'].includes(operation.state)
    && Array.isArray(operation.gaps) && operation.gaps.every(gap => typeof gap === 'string')
    && (!original.operation || original.operation.operation_id === operation.operation_id);
}
export function readDevOpsSetupReceipt(storageKey: string, ownerKey: string): DevOpsSetupReceipt | null {
  const raw = sessionStorage.getItem(storageKey);
  if (!raw) return null;
  const saved = JSON.parse(raw) as DevOpsSetupReceipt;
  const request = saved?.request;
  const owner = JSON.parse(ownerKey) as unknown[];
  const snapshot = typeof saved?.configurationKey === 'string' ? JSON.parse(saved.configurationKey) as unknown[] : null;
  const historicalScope = snapshot?.[3] as DevOpsConfiguration['scope'] | undefined;
  if (saved?.ownerKey !== ownerKey || !/^[A-Za-z0-9_-]{16,128}$/.test(saved?.key)
    || !Array.isArray(snapshot) || snapshot.length !== 5 || snapshot[0] !== owner[4]
    || !Number.isInteger(snapshot[1]) || (snapshot[1] as number) < 1 || typeof snapshot[2] !== 'string' || !snapshot[2]
    || !historicalScope || historicalScope.tenant_id !== owner[0] || historicalScope.product_id !== owner[1]
    || historicalScope.project_id !== owner[2] || historicalScope.owner_id !== owner[3]
    || !request || !['create', 'link'].includes(request.action) || request.configuration_revision !== snapshot[1]
    || !sameConsentScope(Object.keys(request).sort(), (request.action === 'create'
      ? ['action', 'configuration_revision'] : ['action', 'configuration_revision', 'native_resource_ref']).sort())
    || !sameConsentScope(snapshot[4], request.action === 'create' ? { action: 'create' }
      : { action: 'link', native_resource_ref: request.native_resource_ref })
    || !['pending', 'confirmed', 'rejected'].includes(saved.state)
    || (saved.state === 'confirmed' && !saved.operation)
    || (saved.operation && !setupOperationMatches(saved.operation, saved))) throw new Error('Invalid retained DevOps setup request.');
  if (request.action === 'link') {
    const ref = request.native_resource_ref;
    if (!ref || !sameConsentScope(Object.keys(ref).sort(),
      ['tenant_id', 'project_id', 'source_connection_id', 'record_type', 'record_id', 'record_revision'].sort())
      || Object.values(ref).some(value => typeof value !== 'string' || !value.trim())
      || ref.tenant_id !== owner[0] || ref.project_id !== owner[2] || ref.record_type !== 'agent_space'
      || !/^[A-Za-z0-9_-]{1,128}$/.test(ref.record_id) || ref.record_revision !== ref.record_id) {
      throw new Error('Invalid retained DevOps adoption target.');
    }
  }
  return saved;
}
export function retainDevOpsSetupReceipt(storageKey: string, receipt: DevOpsSetupReceipt) {
  // Keep the earlier rejected receipt if a later, explicit setup request is made.
  const serialized = JSON.stringify(receipt);
  sessionStorage.setItem(`${storageKey}:${receipt.key}`, serialized);
  sessionStorage.setItem(storageKey, serialized);
}
