import { apiClient, buildQueryString } from './api';

export interface CatalogTool {
  name: string;
  description: string;
  connector: string;
  credential_kinds: string[];
}

export interface CatalogConnector {
  name: string;
  credential_kinds: string[];
  dynamic: boolean;
}

export interface ToolCatalog {
  tools: CatalogTool[];
  connectors: CatalogConnector[];
}

export interface ToolEntitlement {
  team_id: string | null;
  tool_name: string;
  enabled: boolean;
  max_cost_usd: string | null;
  revision: number;
}

export interface EntitlementList {
  tenant_id: string;
  team_id: string | null;
  entitlements: ToolEntitlement[];
}

export interface EntitlementWrite {
  tenant_id: string;
  team_id: string | null;
  tool_name: string;
  enabled: boolean;
  max_cost_usd: string | null;
  expected_revision: number;
}

export function getToolsCatalog(tenantId: string): Promise<ToolCatalog> {
  return apiClient.get(`/admin/tools/catalog${buildQueryString({ tenant_id: tenantId })}`);
}

export function getToolEntitlements(tenantId: string, teamId: string | null): Promise<EntitlementList> {
  return apiClient.get(`/admin/tools/entitlements${buildQueryString({ tenant_id: tenantId, team_id: teamId })}`);
}

export function putToolEntitlement(body: EntitlementWrite): Promise<ToolEntitlement> {
  return apiClient.put('/admin/tools/entitlements', body);
}


export type ConsentPosture = 'granted' | 'explicit';

/** #8387: the tenant's tools consent posture — its own override or the platform default. */
export interface ToolsConsentDefault {
  tenant_id: string;
  consent_default: ConsentPosture;
  tenant_override: ConsentPosture | null;
  platform_default: ConsentPosture;
  choices: ConsentPosture[];
}

export function getToolsConsentDefault(tenantId: string): Promise<ToolsConsentDefault> {
  return apiClient.get(`/admin/tools/consent-default${buildQueryString({ tenant_id: tenantId })}`);
}

export function putToolsConsentDefault(tenantId: string, consentDefault: ConsentPosture | null): Promise<ToolsConsentDefault> {
  return apiClient.put('/admin/tools/consent-default', { tenant_id: tenantId, consent_default: consentDefault });
}

export interface ToolAccessReport {
  schema_version: '1.0';
  tenant_id: string;
  user_id: string;
  team_id: string;
  consent_mode?: 'default' | 'explicit';
  tools: { name: string; ready: boolean; reason: string | null; discovery_reason?: string | null; max_cost_usd: string | null }[];
}

export function getToolAccess(tenantId: string, userId: string): Promise<ToolAccessReport> {
  return apiClient.get(`/admin/tools/users/${encodeURIComponent(userId)}/access${buildQueryString({ tenant_id: tenantId })}`);
}
