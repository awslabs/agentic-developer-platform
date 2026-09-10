import { apiClient, buildQueryString } from './api';
import type { PaginatedResponse } from '@/types/api';
import { AdminRole } from '@/types';
import type {
  Organization,
  OrganizationCanonicalCreateRequest,
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
      // Issue #4841: the organizations panel distinguishes GitHub-connected from
      // platform-native orgs. Optional here because the field is absent on older
      // responses, and `??  []` in the transform maps that to "none".
      github_installation_ids?: string[];
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
    member_approval_policy?: string;
    created_at: string;
  }>(`/admin/organizations/${id}`);
  return transformOrganization(response);
}

// Issue #4842 (ruling D4 = Option A): `createOrganization()` used to live here,
// posting to `/admin/organizations`. That route now returns 410 — it created an
// `organizations` row without the default department, default team, or channel
// mappings that every other org-creating path writes, which left the installation
// resolver failing closed for tenants made through it.
//
// This function had no product callers (only its own unit test), so it is removed
// rather than repointed: the canonical route
// `POST /api/admin/identity/organizations` takes a DIFFERENT request shape (it
// requires a caller-supplied `id`, plus `plan` and `channels`), so a silent
// repoint would have shipped a client that 422s. The typed client for the canonical
// route (`createOrganizationCanonical`, below) shipped with #4841.

/**
 * Create an organization on the CANONICAL route — Issue #4841 (#4839 · T2a), ruling D4=A.
 *
 * `POST /api/admin/identity/organizations`. This is the route that produces a *complete*
 * org, in one transaction: the org row, a default department (`{id}-dept-default`), a
 * default team (`{id}-team-default`), and the channel mappings
 * (`src/admin/identity/organizations_service.py`). The deprecated sibling above creates
 * only the org row, which is why targeting the wrong one is silently corrupting rather
 * than merely inconsistent.
 *
 * **The caller supplies `id`.** It is required by the server schema and immutable after
 * create — see `utils/orgIdentifier.ts` for the derive-from-name-and-confirm affordance
 * that produces it.
 *
 * **The default department and team are a normal post-condition, not an error.** A caller
 * rendering the structure tree immediately after this resolves will see both, and must
 * present them as ordinary rows (#4841 Design, consequence 2).
 *
 * **Platform-admin only, and not by permission.** The identity router mounts under
 * `dependencies=[Depends(require_admin)]` (`src/admin/identity/router.py`), and
 * `require_admin` checks `is_admin`, which deliberately EXCLUDES `org_admin`
 * (`src/auth/dependencies.py`). So this is not the `Permission.ORG_CREATE` /
 * `target_org_id` scoped check the dept/team routes use — an org admin calling it gets a
 * flat 403 no matter which org they name. Callers gate the affordance on
 * `isPlatformAdmin()`, matching the route rather than the permission enum.
 *
 * Errors are surfaced, not swallowed: the router wraps every failure as a 409 with the
 * underlying message (including a duplicate id), and that message is the only thing that
 * tells an admin their identifier is taken.
 */
// QUARANTINED-DOUBLE-PREFIX-CALLER (#4330 follow-up): this hits the identity router's
// double-prefix mount (`/api/admin/identity/...` behind CloudFront's stripped `/api`).
// The `/api` here is doubled ON PURPOSE — apiClient's base is already `/api`, so the
// browser emits `/api/api/...` and CloudFront strips one segment back to the mount.
// Dropping one `/api` 404s. See tests/test_route_prefix_convention.py's
// QUARANTINED_API_PREFIXED_PATHS, which lists this exact path. When the planned remount
// lands, THIS call site must change in the same commit as the router — grep this marker.
export async function createOrganizationCanonical(
  data: OrganizationCanonicalCreateRequest
): Promise<{ id: string; name: string }> {
  const response = await apiClient.post<{
    id: string;
    name: string;
  }>('/api/admin/identity/organizations', data);
  return { id: response.id, name: response.name };
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

// Get the roles the CURRENT CALLER may assign. Issue #4019: the backend now
// ceiling-filters this list, so an org admin never sees `platform_admin` (which
// it would be rejected for submitting).
export async function getAvailableRoles(): Promise<string[]> {
  const response = await apiClient.get<{ roles: string[] }>('/admin/users/roles');
  return response.roles;
}

// Issue #4019: these two were `throw new Error(...)` placeholders, so role
// management was unreachable through the product — role changes happened only
// out-of-band via Cognito CLI scripts.
//
// Both go through PUT /admin/organizations/{orgId}/users/{userId}, which writes
// the `tenant_memberships` row that actually confers authority (a Cognito-only
// write would display as a promotion while granting nothing).
export async function assignUserRole(data: UserRoleAssignRequest): Promise<UserRole> {
  if (!data.org_id) {
    throw new Error('org_id is required to change a user role');
  }
  const response = await apiClient.put<{
    id: string;
    org_id: string;
    role: string | null;
    created_at: string;
  }>(`/admin/organizations/${data.org_id}/users/${data.user_id}`, { role: data.role });

  return {
    userId: response.id,
    role: response.role as UserRole['role'],
    orgId: response.org_id,
    deptId: null,
    permissions: [],
    createdAt: response.created_at,
  };
}

// "Remove role" means DEMOTE TO MEMBER, never delete the membership row.
// Deleting it makes the user a no-row principal, which log-spams
// `rbac_role_fallback` on every request and would silently regain ORG_ADMIN if
// the backend's least-privilege default were ever rolled back.
export async function removeUserRole(userId: string, orgId?: string): Promise<void> {
  if (!orgId) {
    throw new Error('org_id is required to remove a user role');
  }
  await apiClient.put(`/admin/organizations/${orgId}/users/${userId}`, {
    role: AdminRole.MEMBER,
  });
}

// Transform functions
function transformOrganization(data: {
  id: string;
  name: string;
  aws_accounts: string[];
  role_mappings: Record<string, string>;
  settings: Record<string, unknown>;
  member_approval_policy?: string;
  // Issue #4841: already on the server's `OrganizationResponse` (admin/schemas.py) and
  // previously dropped by this transform. The organizations panel needs it to tell a
  // GitHub-connected org from a platform-native one — mapping "no GitHub" to the same
  // rendering as "we didn't ask" is the R1 distinction the panel exists to show.
  github_installation_ids?: string[];
  created_at: string;
}): Organization {
  return {
    id: data.id,
    name: data.name,
    awsAccounts: data.aws_accounts,
    roleMappings: data.role_mappings,
    settings: data.settings,
    memberApprovalPolicy: data.member_approval_policy,
    githubInstallationIds: data.github_installation_ids ?? [],
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
 * Get the organization's members from Postgres, including their Cognito sub.
 *
 * Issue #4511: `getCognitoUsers` above returns a Cognito-shaped record whose
 * `username` is `GitHub_<github_id>` for GitHub-onboarded users — NOT the sub.
 * Budget and usage records key `user` entities by the Cognito sub, so anything
 * that needs a usable user key must read it from here, where `cognito_sub` is
 * carried explicitly. `cognitoSub` is nullable: members who have never signed
 * in have no sub, and callers must not treat them as selectable.
 */
export async function getOrgUsers(
  orgId: string,
  params?: { page?: number; pageSize?: number }
): Promise<{
  items: Array<{
    id: string;
    email: string;
    name: string | null;
    cognitoSub: string | null;
    role: string | null;
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
      id: string;
      email: string;
      name: string | null;
      cognito_sub: string | null;
      role: string | null;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/organizations/${orgId}/users${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map((user) => ({
      id: user.id,
      email: user.email,
      name: user.name,
      cognitoSub: user.cognito_sub,
      role: user.role,
    })),
    total: response?.total ?? 0,
    page: response?.page ?? 1,
    pageSize: response?.page_size ?? 50,
    hasMore: response?.has_more ?? false,
  };
}

/** One row of the platform-wide member listing — Issue #4827. */
export interface PlatformUser {
  /**
   * The canonical `users.id`.
   *
   * This is the value a person-scoped rule must be stored under: the routing
   * resolver and the server's `require_scope_exists` both compare `scope_id_user`
   * to this column (#4647). A picker submitting anything else — a Cognito sub, a
   * GitHub login — authors a rule that reads back correctly and governs nobody.
   */
  id: string;
  orgId: string;
  email: string;
  name: string | null;
  /** The linked GitHub login, or `null` for a member with no GitHub identity. */
  githubUsername: string | null;
}

/**
 * Every platform member, paginated and searchable. Platform-admin only.
 *
 * Issue #4827. Every other member listing in this service is per-org
 * (`getOrgUsers`, `getCognitoUsers`) because every other caller is authoring
 * something inside one org. A platform admin authoring a person-scoped Bedrock
 * routing rule may pin any user in any org, so an org-scoped list would hide
 * exactly the people that authority covers — which is how the person field ended
 * up being a UUID typed by hand.
 *
 * `q` is a server-side search over email, display name, and GitHub username.
 * Callers debounce it and render one page: pulling the whole member table into the
 * browser on mount is what this endpoint's pagination exists to avoid.
 *
 * The caller-side platform-admin check is an affordance, never the boundary —
 * `require_platform_admin` gates the route server-side. A 403 here is a real
 * refusal and must surface, not be rendered as an empty roster.
 */
export async function listPlatformUsers(params?: {
  q?: string;
  page?: number;
  pageSize?: number;
}): Promise<{
  items: PlatformUser[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
}> {
  const query = buildQueryString({
    q: params?.q?.trim() || undefined,
    page: params?.page || 1,
    page_size: params?.pageSize || 50,
  });
  const response = await apiClient.get<{
    items: Array<{
      id: string;
      org_id: string;
      email: string;
      name: string | null;
      github_username: string | null;
    }>;
    total: number;
    page: number;
    page_size: number;
    has_more: boolean;
  }>(`/admin/users${query}`);

  const items = Array.isArray(response?.items) ? response.items : [];
  return {
    items: items.map((user) => ({
      id: user.id,
      orgId: user.org_id,
      email: user.email,
      name: user.name,
      githubUsername: user.github_username,
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

/**
 * The GitHub numeric user id linked to a member, or `null` when none is.
 *
 * Issue #4687. Authoring somebody's cross-workspace person limit needs their person
 * anchor (`github:<numeric id>`), and this is the only client-side way to obtain the
 * id inside it **from the server**. That "from the server" is the requirement, not a
 * convenience: the anchor is the key the cap is stored under, so an id derived from
 * anything the operator typed — or inferred client-side from a display name, an email,
 * or a `users.id` — produces a row that validates, displays a limit, and is never
 * matched by the enforcement path (#4511, and #4629's reason for existing at all).
 *
 * `users.cognito_username` looks like a shortcut here (`GitHub_<id>` for
 * GitHub-onboarded members, already on the member-list payload) and is deliberately
 * NOT used: it is only populated on the admin-invite path, so it is NULL for exactly
 * the population #4511 affected. `user_identities` is the bridge that is actually
 * populated, and this endpoint reads it.
 *
 * Returns `null` rather than throwing for "no GitHub identity", because that is a
 * legitimate, permanent state for a member who signed up by email — the caller renders
 * the same explanation the self-service card uses instead of an error. A transport or
 * authorization failure still throws: "we could not ask" must not render as "they have
 * no GitHub account".
 *
 * The `/api` prefix is doubled on purpose. This router mounts at
 * `/api/admin/identity/*` while `apiClient`'s base is already `/api`, so the browser
 * emits `/api/api/...` and CloudFront strips one segment back to the mount — the same
 * compensating double prefix `knowledge.ts` uses and the quarantine list in
 * `tests/test_route_prefix_convention.py` documents. Dropping one `/api` here 404s.
 */
// QUARANTINED-DOUBLE-PREFIX-CALLER (#4330 follow-up): this hits the identity
// router's double-prefix mount (`/api/admin/identity/...` behind CloudFront's
// stripped `/api`). When the planned remount lands, THIS call site must change
// in the same commit as the router — grep this marker; the backend
// route-prefix test's scan does not cover frontend callers (review note on
// #4688).
export async function getMemberGithubUserId(userId: string): Promise<string | null> {
  const response = await apiClient.get<{
    identities: Array<{ provider: string; provider_user_id: string }>;
    total: number;
  }>(`/api/admin/identity/users/${userId}/identities`);

  const identities = Array.isArray(response?.identities) ? response.identities : [];
  const github = identities.find((i) => i.provider?.toLowerCase() === 'github');
  // An empty/whitespace `provider_user_id` is treated as "not linked" rather than
  // passed through: `github:` with nothing after it is precisely one of the three
  // shapes `parse_person_anchor` rejects, and failing here explains itself while
  // failing there surfaces as a bare 422.
  const providerUserId = github?.provider_user_id?.trim();
  return providerUserId ? providerUserId : null;
}
