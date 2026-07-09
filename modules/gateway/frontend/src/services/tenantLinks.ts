/**
 * API service for tenant org-linking (rule 3).
 *
 * Issue #2954: Platform-admin can link/unlink GitHub orgs to a parent tenant.
 */

import { apiClient } from './api';

export interface LinkedOrgItem {
  orgId: string;
  orgName: string;
  githubOrgId: string | null;
}

export interface LinkedOrgsListResponse {
  tenantId: string;
  linkedOrgs: LinkedOrgItem[];
}

export interface LinkOrgResponse {
  linked: boolean;
  tenantId: string;
  githubOrgId: string;
  orgName: string;
}

export interface UnlinkOrgResponse {
  unlinked: boolean;
  tenantId: string;
  githubOrgId: string;
}

/**
 * List all orgs linked to a tenant.
 */
export async function getLinkedOrgs(tenantId: string): Promise<LinkedOrgsListResponse> {
  const response = await apiClient.get<{
    tenant_id: string;
    linked_orgs: Array<{
      org_id: string;
      org_name: string;
      github_org_id: string | null;
    }>;
  }>(`/admin/tenants/${tenantId}/orgs`);

  return {
    tenantId: response.tenant_id,
    linkedOrgs: (response.linked_orgs || []).map((org) => ({
      orgId: org.org_id,
      orgName: org.org_name,
      githubOrgId: org.github_org_id,
    })),
  };
}

/**
 * Link a GitHub org to a parent tenant.
 */
export async function linkOrgToTenant(
  tenantId: string,
  githubOrgId: string,
): Promise<LinkOrgResponse> {
  const response = await apiClient.post<{
    linked: boolean;
    tenant_id: string;
    github_org_id: string;
    org_name: string;
  }>(`/admin/tenants/${tenantId}/orgs`, { github_org_id: githubOrgId });

  return {
    linked: response.linked,
    tenantId: response.tenant_id,
    githubOrgId: response.github_org_id,
    orgName: response.org_name,
  };
}

/**
 * Unlink a GitHub org from a parent tenant.
 */
export async function unlinkOrgFromTenant(
  tenantId: string,
  githubOrgId: string,
): Promise<UnlinkOrgResponse> {
  const response = await apiClient.delete<{
    unlinked: boolean;
    tenant_id: string;
    github_org_id: string;
  }>(`/admin/tenants/${tenantId}/orgs/${githubOrgId}`);

  return {
    unlinked: response.unlinked,
    tenantId: response.tenant_id,
    githubOrgId: response.github_org_id,
  };
}
