// Admin roles matching backend
export enum AdminRole {
  PLATFORM_ADMIN = 'platform_admin',
  ORG_ADMIN = 'org_admin',
  DEPT_ADMIN = 'dept_admin',
}

// Permissions matching backend
export enum Permission {
  ORG_CREATE = 'org:create',
  ORG_READ = 'org:read',
  ORG_UPDATE = 'org:update',
  ORG_DELETE = 'org:delete',
  BUDGET_READ = 'budget:read',
  BUDGET_UPDATE = 'budget:update',
  RATELIMIT_READ = 'ratelimit:read',
  RATELIMIT_UPDATE = 'ratelimit:update',
  POOL_READ = 'pool:read',
  POOL_MANAGE = 'pool:manage',
  USAGE_READ = 'usage:read',
  LOGS_READ = 'logs:read',
  LOGS_EXPORT = 'logs:export',
  USER_READ = 'user:read',
  USER_MANAGE = 'user:manage',
  METRICS_READ = 'metrics:read',
}

// Period types for budgets
export enum PeriodType {
  DAILY = 'daily',
  WEEKLY = 'weekly',
  MONTHLY = 'monthly',
}

// Entity types
export enum EntityType {
  ORGANIZATION = 'org',
  DEPARTMENT = 'department',
  TEAM = 'team',
  USER = 'user',
  SERVICE_ACCOUNT = 'service_account',
}

// Enforcement modes
export enum EnforcementMode {
  SOFT = 'soft',
  HARD = 'hard',
}

// Pool account status
export enum PoolAccountStatus {
  HEALTHY = 'healthy',
  UNHEALTHY = 'unhealthy',
  UNKNOWN = 'unknown',
}

// User and authentication types
export interface User {
  id: string;
  email?: string;
  name?: string;
  // Undefined when the JWT carries no custom:role claim (e.g. a freshly
  // signed-up GitHub user who hasn't been approved + assigned yet). UI
  // should hide the role label in that case rather than default to a
  // misleading "org admin" badge.
  role?: AdminRole;
  orgId?: string;
  deptId?: string;
  permissions: Permission[];
  createdAt: string;
  avatarUrl?: string;
  githubLogin?: string;
}

export interface AuthState {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
}

// Legacy auth types - kept for backwards compatibility during migration
// TODO: Remove after full migration to Cognito OAuth
export interface AuthExchangeRequest {
  aws_access_key_id: string;
  aws_secret_access_key: string;
  aws_session_token: string;
}

export interface AuthExchangeResponse {
  token: string;
  expires_at: string;
  user_id: string;
  org_id: string;
  team_id: string;
  department_id: string;
  account_type: 'human' | 'service';
}

export interface TokenContext {
  userId: string;
  orgId: string;
  teamId: string;
  departmentId: string;
  accountType: 'human' | 'service';
  isAdmin: boolean;
  expiresAt: string;
}

// Cognito OAuth 2.0 PKCE Types
export interface CognitoTokenResponse {
  access_token: string;
  id_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: 'Bearer';
}

export interface PKCEChallenge {
  verifier: string;
  challenge: string;
}

export interface CognitoConfig {
  userPoolId: string;
  clientId: string;
  domain: string;
  region: string;
  redirectUri: string;
}

export interface CognitoIdTokenPayload {
  sub: string;
  email: string;
  email_verified: boolean;
  name?: string;
  picture?: string;
  'cognito:username': string;
  'custom:org_id'?: string;
  'custom:department_id'?: string;
  'custom:team_id'?: string;
  'custom:role'?: string;
  identities?: string;
  iss: string;
  aud: string;
  exp: number;
  iat: number;
  auth_time: number;
  token_use: 'id';
}

export interface CognitoAccessTokenPayload {
  sub: string;
  'cognito:groups'?: string[];
  iss: string;
  client_id: string;
  origin_jti: string;
  event_id: string;
  token_use: 'access';
  scope: string;
  auth_time: number;
  exp: number;
  iat: number;
  jti: string;
  username: string;
}

// Organization types
export interface Organization {
  id: string;
  name: string;
  awsAccounts: string[];
  roleMappings: Record<string, string>;
  settings: Record<string, unknown>;
  createdAt: string;
}

export interface OrganizationCreateRequest {
  name: string;
  aws_accounts?: string[];
  role_mappings?: Record<string, string>;
  settings?: Record<string, unknown>;
}

export interface OrganizationUpdateRequest {
  name?: string;
  aws_accounts?: string[];
  role_mappings?: Record<string, string>;
  settings?: Record<string, unknown>;
}

// Department types
export interface Department {
  id: string;
  orgId: string;
  name: string;
  description?: string;
  createdAt: string;
}

// Team types
export interface Team {
  id: string;
  departmentId: string;
  name: string;
  description?: string;
  createdAt: string;
}

// Pool types
export interface PoolAccount {
  id: string;
  accountId: string;
  roleArn: string;
  region: string;
  isHealthy: boolean;
  lastHealthCheck: string | null;
  createdAt: string;
}

export interface PoolStatus {
  totalAccounts: number;
  healthyAccounts: number;
  unhealthyAccounts: number;
  accounts: PoolAccount[];
}

export interface PoolAccountCreateRequest {
  account_id: string;
  role_arn: string;
  region?: string;
}

// Budget types
export interface Budget {
  id: string;
  entityType: EntityType;
  entityId: string;
  periodType: PeriodType;
  budgetAmountUsd: number;
  enforcementMode: EnforcementMode;
  orgId: string;
  updatedAt: string;
}

export interface BudgetStatus {
  budgetAmountUsd: number;
  currentSpendUsd: number;
  remainingBudgetUsd: number;
  budgetUtilizationPercent: number;
  periodStart: string;
  periodEnd: string;
  periodType: PeriodType;
  enforcementMode: EnforcementMode;
  budgetExceeded: boolean;
  warnings: string[];
}

export interface BudgetUsage {
  id: string;
  entityType: EntityType;
  entityId: string;
  periodStart: string;
  periodType: PeriodType;
  totalCostUsd: number;
  totalTokens: number;
  requestCount: number;
  orgId: string;
}

export interface BudgetCreateRequest {
  entity_type: EntityType;
  entity_id: string;
  period_type: PeriodType;
  budget_amount_usd: number;
  enforcement_mode?: EnforcementMode;
}

export interface BudgetUpdateRequest {
  budget_amount_usd?: number;
  enforcement_mode?: EnforcementMode;
}

// Rate limit types
export interface RateLimitConfig {
  orgId: string;
  entityType: string;
  entityId: string;
  rpm: number | null;
  tpm: number | null;
  concurrentRequests: number | null;
  updatedAt: string;
}

export interface RateLimitUpdateRequest {
  rpm?: number | null;
  tpm?: number | null;
  concurrent_requests?: number | null;
}

// Log types
export interface LogEntry {
  id: string;
  timestamp: string;
  orgId: string;
  userId: string;
  method: string;
  path: string;
  statusCode: number;
  responseTimeMs: number;
  requestBodySize: number | null;
  responseBodySize: number | null;
}

export interface LogQueryRequest {
  start_time?: string;
  end_time?: string;
  org_id?: string;
  user_id?: string;
  status_code?: number;
  path_pattern?: string;
  min_response_time_ms?: number;
  page?: number;
  page_size?: number;
}

// Health check types
export interface HealthCheckComponent {
  name: string;
  status: string;
  latencyMs: number | null;
  error: string | null;
}

export interface HealthCheckResponse {
  status: string;
  timestamp: string;
  components: HealthCheckComponent[] | null;
}

// Dashboard types
export interface PlatformDashboard {
  totalOrganizations: number;
  totalRequests24h: number;
  totalTokens24h: number;
  totalCost24h: number;
  activeUsers24h: number;
  errorRate24h: number;
  poolStatus: PoolStatus;
  topOrganizations: Array<{
    id: string;
    name: string;
    requestCount: number;
    tokenCount: number;
    costUsd: number;
  }>;
}

export interface OrgDashboard {
  orgId: string;
  orgName: string;
  totalRequests24h: number;
  totalTokens24h: number;
  totalCost24h: number;
  activeUsers24h: number;
  errorRate24h: number;
  budgetStatus?: BudgetStatus;
  topDepartments: Array<{
    id: string;
    name: string;
    requestCount: number;
    tokenCount: number;
    costUsd: number;
  }>;
  topModels: Array<{
    name: string;
    requestCount: number;
    tokenCount: number;
    costUsd: number;
  }>;
}

// User role types
export interface UserRole {
  userId: string;
  role: AdminRole;
  orgId: string | null;
  deptId: string | null;
  permissions: Permission[];
  createdAt: string;
}

export interface UserRoleAssignRequest {
  user_id: string;
  role: AdminRole;
  org_id?: string | null;
  dept_id?: string | null;
}

// System metrics
export interface SystemMetrics {
  apiCallsPerMinute: number;
  averageLatencyMs: number;
  errorRate: number;
  activeConnections: number;
  cpuUtilization: number;
  memoryUtilization: number;
}

// Usage data for charts
export interface UsageDataPoint {
  timestamp: string;
  requestCount: number;
  tokenCount: number;
  costUsd: number;
  errorCount: number;
}

// =============================================================================
// Cognito Entity Types (Issue #226)
// =============================================================================

/**
 * User data from Cognito.
 *
 * Issue #226: Cognito as single source of truth for users.
 */
export interface CognitoUser {
  username: string;
  email: string | null;
  name: string | null;
  orgId: string | null;
  departmentId: string | null;
  teamId: string | null;
  role: string | null;
  status: string | null;
  enabled: boolean;
  createdAt: string | null;
  updatedAt: string | null;
}

/**
 * Team/group data from Cognito.
 *
 * Issue #226: Cognito groups represent teams.
 */
export interface CognitoTeam {
  groupName: string;
  description: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}

/**
 * Department data derived from Cognito users.
 *
 * Issue #226: Departments are derived from custom:department_id attribute.
 */
export interface CognitoDepartment {
  departmentId: string;
}

// ---------------------------------------------------------------------------
// Issue #1424: Knowledge-layer indexing status types
// ---------------------------------------------------------------------------

/** A single stage row from index_run_stages. */
export interface IndexRunStage {
  id: string;
  runId: string;
  repo: string;
  stage: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'verified' | 'skipped';
  artifactRef: string | null;
  verifiedAt: string | null;
  attempts: number;
  error: string | null;
  startedAt: string | null;
  completedAt: string | null;
}

/** Level 1 — one row per index run. */
export interface IndexRunSummary {
  id: string;
  repoId: string;
  startedAt: string;
  completedAt: string | null;
  durationMs: number | null;
  status: string;
  commitSha: string | null;
  error: string | null;
  totalRepos: number;
  reposVerified: number;
  reposFailed: number;
  reposPartial: number;
}

/** Top-level summary stats for StatCards. */
export interface IndexingSummaryStats {
  totalRepos: number;
  fullyVerifiedPct: number;
  failedStages: number;
  driftCount: number;
}

/** Paginated list of index runs (Level 1 response). */
export interface IndexRunListResponse {
  items: IndexRunSummary[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  summary: IndexingSummaryStats | null;
}

/** Level 2 — per-repo, per-stage detail for a single run. */
export interface IndexRunDetailResponse {
  runId: string;
  startedAt: string;
  completedAt: string | null;
  status: string;
  commitSha: string | null;
  stages: IndexRunStage[];
}

// ---------------------------------------------------------------------------
// Issue #1794: Knowledge-assets management page types
// ---------------------------------------------------------------------------

/** Asset status values. */
export type AssetStatus = 'registered' | 'queued' | 'indexing' | 'indexed' | 'failed' | 'removed';

/** A single knowledge asset. */
export interface KnowledgeAsset {
  id: string;
  assetType: string;
  sourceRef: string;
  displayName: string | null;
  tags: Record<string, unknown>;
  metadata: Record<string, unknown>;
  tenantId: string | null;
  ownerSub: string | null;
  projectId: string | null;
  status: AssetStatus;
  lastError: string | null;
  retryCount: number;
  registeredBy: string | null;
  createdAt: string;
  updatedAt: string | null;
}

/** Quota detail for a single asset type. */
export interface AssetQuotaDetail {
  used: number;
  limit: number;
}

/** Aggregated quota info. */
export interface AssetQuotaInfo {
  repos: AssetQuotaDetail | null;
  urls: AssetQuotaDetail | null;
  docs: AssetQuotaDetail | null;
}

/** Paginated asset list response. */
export interface AssetListResponse {
  items: KnowledgeAsset[];
  total: number;
  page: number;
  pageSize: number;
  hasMore: boolean;
  quota: AssetQuotaInfo | null;
}

/** Request body for creating an asset. */
export interface AssetCreateRequest {
  asset_type: string;
  source_ref: string;
  display_name?: string;
  tags?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  scope: 'personal' | 'tenant';
}

/** A GitHub repo from the picker API. */
export interface AccessibleRepo {
  fullName: string;
  private: boolean;
  url: string;
}

/** Response from the repo picker API. */
export interface AccessibleReposResponse {
  repos: AccessibleRepo[];
  total: number;
  page: number;
  hasMore: boolean;
}
