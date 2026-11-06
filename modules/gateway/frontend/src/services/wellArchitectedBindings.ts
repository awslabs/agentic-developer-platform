import { apiClient } from './api';
import { wellArchitectedPath, type SecurityBinding, type SecurityBindingOperation } from './l5Services';

export interface WellArchitectedCheckRequest {
  action: 'check'; configuration_revision: number; binding_id: string;
}

export const readWellArchitectedBinding = (productId: string, projectId: string, bindingId: string) =>
  apiClient.requestWithEtag<SecurityBinding>(`${wellArchitectedPath(productId, projectId)}/bindings/${encodeURIComponent(bindingId)}`);

export const checkWellArchitectedBinding = (
  productId: string, projectId: string, body: WellArchitectedCheckRequest, etag: string, key: string,
) => apiClient.requestWithEtag<SecurityBindingOperation>(`${wellArchitectedPath(productId, projectId)}/binding-operations`, {
  method: 'POST', headers: { 'Idempotency-Key': key, 'If-Match': etag }, body,
});
