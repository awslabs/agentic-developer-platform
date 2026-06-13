import { apiClient, buildQueryString } from './api';
import type { PaginatedResponse } from '@/types/api';
import type {
  Organization,
  OrganizationCreateRequest,
  OrganizationUpdateRequest,
  Department,
  Team,
  UserRole,
  UserRoleAssignRequest,
  IndexRunListResponse,
  IndexRunDetailResponse,
} from '@/types';

// Organization endpoints
export async function getOrganizations(params?: {
  page?: number;
  pageSize?: number;
}): Promise<PaginatedResponse<Organization>> {
  const query = buildQueryString({
    page: params?.page || 1,
    page_size: params?.pageSize || 50,
  });
  const response = await apiClient.get<{
    items: Array<{
      id: string;
      name: string;
      aws_accounts: string[];
      role_mappings: Record<string, string>;
      settings: Record<string, unknown>;
      created_at: string;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map(transformOrganization),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 50,
    hasMore: response?.has_more ?? false,
  };
}

export async function getOrganization(id: string): Promise<Organization> {
  const response = await apiClient.get<{
    id: string;
    name: string;
    aws_accounts: string[];
    role_mappings: Record<string, string>;
    settings: Record<string, unknown>;
    created_at: string;
  }>(`/admin/organizations/${id}`);
  return transformOrganization(response);
}

export async function createOrganization(data: OrganizationCreateRequest): Promise<Organization> {
  const response = await apiClient.post<{
    id: string;
    name: string;
    aws_accounts: string[];
    role_mappings: Record<string, string>;
    settings: Record<string, unknown>;
    created_at: string;
  }>('/admin/organizations', data);
  return transformOrganization(response);
}

export async function updateOrganization(
  id: string,
  data: OrganizationUpdateRequest
): Promise<Organization> {
  const response = await apiClient.put<{
    id: string;
    name: string;
    aws_accounts: string[];
    role_mappings: Record<string, string>;
    settings: Record<string, unknown>;
    created_at: string;
  }>(`/admin/organizations/${id}`, data);
  return transformOrganization(response);
}

export async function deleteOrganization(id: string): Promise<void> {
  await apiClient.delete(`/admin/organizations/${id}`);
}

// Department endpoints
export async function getDepartments(
  orgId: string,
  params?: { page?: number; pageSize?: number }
): Promise<PaginatedResponse<Department>> {
  const query = buildQueryString({
    page: params?.page || 1,
    page_size: params?.pageSize || 50,
  });
  const response = await apiClient.get<{
    items: Array<{
      id: string;
      org_id: string;
      name: string;
      description?: string;
      created_at: string;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/departments${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map(transformDepartment),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 50,
    hasMore: response?.has_more ?? false,
  };
}

export async function createDepartment(
  orgId: string,
  data: { name: string; description?: string }
): Promise<Department> {
  const response = await apiClient.post<{
    id: string;
    org_id: string;
    name: string;
    description?: string;
    created_at: string;
  }>(`/admin/organizations/${orgId}/departments`, data);
  return transformDepartment(response);
}

export async function updateDepartment(
  orgId: string,
  deptId: string,
  data: { name?: string; description?: string }
): Promise<Department> {
  const response = await apiClient.put<{
    id: string;
    org_id: string;
    name: string;
    description?: string;
    created_at: string;
  }>(`/admin/organizations/${orgId}/departments/${deptId}`, data);
  return transformDepartment(response);
}

export async function deleteDepartment(orgId: string, deptId: string): Promise<void> {
  await apiClient.delete(`/admin/organizations/${orgId}/departments/${deptId}`);
}

// Team endpoints
export async function getTeams(
  orgId: string,
  deptId: string,
  params?: { page?: number; pageSize?: number }
): Promise<PaginatedResponse<Team>> {
  const query = buildQueryString({
    page: params?.page || 1,
    page_size: params?.pageSize || 50,
  });
  const response = await apiClient.get<{
    items: Array<{
      id: string;
      department_id: string;
      name: string;
      description?: string;
      created_at: string;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/departments/${deptId}/teams${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map(transformTeam),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 50,
    hasMore: response?.has_more ?? false,
  };
}

export async function createTeam(
  orgId: string,
  deptId: string,
  data: { name: string; description?: string }
): Promise<Team> {
  const response = await apiClient.post<{
    id: string;
    department_id: string;
    name: string;
    description?: string;
    created_at: string;
  }>(`/admin/organizations/${orgId}/departments/${deptId}/teams`, data);
  return transformTeam(response);
}

export async function updateTeam(
  orgId: string,
  teamId: string,
  data: { name?: string; description?: string }
): Promise<Team> {
  // Note: Backend team update endpoint is /admin/organizations/{org_id}/teams/{team_id}
  const response = await apiClient.put<{
    id: string;
    org_id: string;
    department_id: string;
    name: string;
    description?: string;
    created_at: string;
    updated_at: string;
  }>(`/admin/organizations/${orgId}/teams/${teamId}`, data);
  return transformTeam({
    id: response.id,
    department_id: response.department_id,
    name: response.name,
    description: response.description,
    created_at: response.created_at,
  });
}

export async function deleteTeam(orgId: string, teamId: string): Promise<void> {
  // Note: Backend team delete endpoint is /admin/organizations/{org_id}/teams/{team_id}
  await apiClient.delete(`/admin/organizations/${orgId}/teams/${teamId}`);
}

// User role endpoints
// Note: The backend doesn't have a dedicated user roles list endpoint.
// Instead, we get users from the organization and transform their role info.
export async function getUserRoles(
  orgId?: string,
  params?: { page?: number; pageSize?: number }
): Promise<PaginatedResponse<UserRole>> {
  if (!orgId) {
    // Without org_id, return empty list
    return {
      items: [],
      total: 0,
      page: 1,
      pageSize: params?.pageSize || 50,
      hasMore: false,
    };
  }

  const query = buildQueryString({
    page: params?.page || 1,
    page_size: params?.pageSize || 50,
  });

  const response = await apiClient.get<{
    items: Array<{
      id: string;
      org_id: string;
      team_id: string;
      email: string;
      name: string;
      role: string;
      cognito_sub: string | null;
      cognito_username: string | null;
      created_at: string;
      updated_at: string;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/users${query}`);

  // Transform user data to UserRole format
  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map((user) => ({
      userId: user.id,
      role: user.role as UserRole['role'],
      orgId: user.org_id,
      deptId: null, // Users are associated with teams, not directly with departments
      permissions: [], // Permissions are derived from role in the backend
      createdAt: user.created_at,
    })),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 50,
    hasMore: response?.has_more ?? false,
  };
}

// Get available roles (static list from backend)
export async function getAvailableRoles(): Promise<string[]> {
  const response = await apiClient.get<{ roles: string[] }>('/admin/users/roles');
  return response.roles;
}

export async function assignUserRole(_data: UserRoleAssignRequest): Promise<UserRole> {
  // Note: User role assignment is done via the user creation/update endpoints
  // This function is a placeholder for future implementation
  throw new Error('User role assignment should be done via user management endpoints');
}

export async function removeUserRole(_userId: string): Promise<void> {
  // Note: User role removal is done via the user deletion endpoint
  // This function is a placeholder for future implementation
  throw new Error('User role removal should be done via user management endpoints');
}

// Transform functions
function transformOrganization(data: {
  id: string;
  name: string;
  aws_accounts: string[];
  role_mappings: Record<string, string>;
  settings: Record<string, unknown>;
  created_at: string;
}): Organization {
  return {
    id: data.id,
    name: data.name,
    awsAccounts: data.aws_accounts,
    roleMappings: data.role_mappings,
    settings: data.settings,
    createdAt: data.created_at,
  };
}

function transformDepartment(data: {
  id: string;
  org_id: string;
  name: string;
  description?: string;
  created_at: string;
}): Department {
  return {
    id: data.id,
    orgId: data.org_id,
    name: data.name,
    description: data.description,
    createdAt: data.created_at,
  };
}

function transformTeam(data: {
  id: string;
  department_id: string;
  name: string;
  description?: string;
  created_at: string;
}): Team {
  return {
    id: data.id,
    departmentId: data.department_id,
    name: data.name,
    description: data.description,
    createdAt: data.created_at,
  };
}

// Note: transformUserRole was removed as user roles are now managed via user management endpoints
// and the getUserRoles function transforms the data inline

// =============================================================================
// Cognito-backed Entity List Functions (Issue #226)
// =============================================================================

/**
 * Get users from Cognito for an organization.
 *
 * Issue #226: Cognito as single source of truth for users.
 * This function fetches users directly from Cognito via the backend API.
 */
export async function getCognitoUsers(
  orgId: string,
  params?: { page?: number; pageSize?: number }
): Promise<{
  items: Array<{
    username: string;
    email: string | null;
    name: string | null;
    githubUsername: string | null;
    orgId: string | null;
    departmentId: string | null;
    teamId: string | null;
    role: string | null;
    status: string | null;
    enabled: boolean;
    createdAt: string | null;
    updatedAt: string | null;
  }>;
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
}> {
  const query = buildQueryString({
    page: params?.page || 1,
    page_size: params?.pageSize || 50,
  });
  const response = await apiClient.get<{
    items: Array<{
      username: string;
      email: string | null;
      name: string | null;
      github_username: string | null;
      org_id: string | null;
      department_id: string | null;
      team_id: string | null;
      role: string | null;
      status: string | null;
      enabled: boolean;
      created_at: string | null;
      updated_at: string | null;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/cognito/users${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map((user) => ({
      username: user.username,
      email: user.email,
      name: user.name,
      githubUsername: user.github_username,
      orgId: user.org_id,
      departmentId: user.department_id,
      teamId: user.team_id,
      role: user.role,
      status: user.status,
      enabled: user.enabled,
      createdAt: user.created_at,
      updatedAt: user.updated_at,
    })),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 50,
    hasMore: response?.has_more ?? false,
  };
}

/**
 * Get teams (Cognito groups) for an organization.
 *
 * Issue #226: Cognito groups represent teams.
 * This function fetches groups directly from Cognito via the backend API.
 */
export async function getCognitoTeams(
  orgId: string,
  params?: { page?: number; pageSize?: number; prefix?: string }
): Promise<{
  items: Array<{
    groupName: string;
    description: string | null;
    createdAt: string | null;
    updatedAt: string | null;
  }>;
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
}> {
  const queryParams: Record<string, unknown> = {
    page: params?.page || 1,
    page_size: params?.pageSize || 50,
  };
  if (params?.prefix) {
    queryParams.prefix = params.prefix;
  }
  const query = buildQueryString(queryParams);
  const response = await apiClient.get<{
    items: Array<{
      group_name: string;
      description: string | null;
      created_at: string | null;
      updated_at: string | null;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/cognito/teams${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map((team) => ({
      groupName: team.group_name,
      description: team.description,
      createdAt: team.created_at,
      updatedAt: team.updated_at,
    })),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 50,
    hasMore: response?.has_more ?? false,
  };
}

/**
 * Get unique departments from Cognito users in an organization.
 *
 * Issue #226: Departments are derived from custom:department_id attribute
 * on users in Cognito.
 */
export async function getCognitoDepartments(orgId: string): Promise<{
  items: Array<{ departmentId: string }>;
  total: number;
}> {
  const response = await apiClient.get<{
    items: Array<{ department_id: string }>;
    total: number;
  }>(`/admin/organizations/${orgId}/cognito/departments`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map((dept) => ({
      departmentId: dept.department_id,
    })),
    total: response?.total ?? 0,
  };
}

// ---------------------------------------------------------------------------
// Issue #1424: Knowledge-layer indexing status endpoints
// ---------------------------------------------------------------------------

export async function getIndexingRuns(params?: {
  page?: number;
  pageSize?: number;
}): Promise<IndexRunListResponse> {
  const query = buildQueryString({
    page: params?.page || 1,
    page_size: params?.pageSize || 20,
  });
  const response = await apiClient.get<{
    items: Array<{
      id: string;
      repo_id: string;
      started_at: string;
      completed_at: string | null;
      duration_ms: number | null;
      status: string;
      commit_sha: string | null;
      error: string | null;
      total_repos: number;
      repos_verified: number;
      repos_failed: number;
      repos_partial: number;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
    summary: {
      total_repos: number;
      fully_verified_pct: number;
      failed_stages: number;
      drift_count: number;
    } | null;
  }>(`/admin/indexing/runs${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map((run) => ({
      id: run.id,
      repoId: run.repo_id,
      startedAt: run.started_at,
      completedAt: run.completed_at,
      durationMs: run.duration_ms,
      status: run.status,
      commitSha: run.commit_sha,
      error: run.error,
      totalRepos: run.total_repos,
      reposVerified: run.repos_verified,
      reposFailed: run.repos_failed,
      reposPartial: run.repos_partial,
    })),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 20,
    hasMore: response?.has_more ?? false,
    summary: response?.summary
      ? {
          totalRepos: response.summary.total_repos,
          fullyVerifiedPct: response.summary.fully_verified_pct,
          failedStages: response.summary.failed_stages,
          driftCount: response.summary.drift_count,
        }
      : null,
  };
}

export async function getIndexingRunDetail(runId: string): Promise<IndexRunDetailResponse> {
  const response = await apiClient.get<{
    run_id: string;
    started_at: string;
    completed_at: string | null;
    status: string;
    commit_sha: string | null;
    stages: Array<{
      id: string;
      run_id: string;
      repo: string;
      stage: string;
      status: string;
      artifact_ref: string | null;
      verified_at: string | null;
      attempts: number;
      error: string | null;
      started_at: string | null;
      completed_at: string | null;
    }>;
  }>(`/admin/indexing/runs/${runId}`);

  return {
    runId: response.run_id,
    startedAt: response.started_at,
    completedAt: response.completed_at,
    status: response.status,
    commitSha: response.commit_sha,
    stages: (response.stages || []).map((s) => ({
      id: s.id,
      runId: s.run_id,
      repo: s.repo,
      stage: s.stage,
      status: s.status as IndexRunDetailResponse['stages'][number]['status'],
      artifactRef: s.artifact_ref,
      verifiedAt: s.verified_at,
      attempts: s.attempts,
      error: s.error,
      startedAt: s.started_at,
      completedAt: s.completed_at,
    })),
  };
}
