/** Knowledge access always uses the gateway's per-run identity bridge. */
import { knowledgeBridgeUrl } from './knowledgeBridge';

export function getDoorBaseUrl(_legacy: string): string {
  return knowledgeBridgeUrl();
}

export function getDoorHeaders(_identity: Record<string, string> = {}): Record<string, string> {
  return {};
}

export function getDoorAuthHeaders(): Record<string, string> {
  return {};
}
