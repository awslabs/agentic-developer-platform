import { apiClient } from './api';

export interface RecordReference {
  tenant_id: string; project_id: string; source_connection_id: string;
  record_type: string; record_id: string; record_revision: string;
}

export interface PortfolioRecord {
  id: string; revision: string; data: { name: string };
}

export interface PortfolioMembership {
  relationship_id: string; relationship_revision: string;
  portfolio_ref: RecordReference; product_ref: RecordReference;
  valid_from: string; valid_to?: string;
}

export interface CatalogPage<T> { items: T[]; next_cursor?: string; can_edit?: boolean }
export interface CatalogMutationReceipt { id: string; revision: string; change_id: string }

export interface InitiativeRecord { tenant_id: string; id: string; revision: string; data: { name: string; status: string } }
export interface ProductContribution {
  id: string; revision: string; project_id: string;
  data: { initiative_ref: RecordReference; product_ref: RecordReference; objective: string; work_links: unknown[] };
}

export const listInitiatives = (cursor?: string) =>
  apiClient.get<CatalogPage<InitiativeRecord>>(`/v1/catalog/initiatives${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);

export const listProductContributions = (projectId: string, cursor?: string) =>
  apiClient.get<CatalogPage<ProductContribution>>(`/v1/catalog/projects/${encodeURIComponent(projectId)}/contributions${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);

export const listPortfolios = (cursor?: string) =>
  apiClient.get<CatalogPage<PortfolioRecord>>(`/v1/catalog/portfolios${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);

export const listPortfolioMemberships = (portfolioId: string, cursor?: string, effectiveAt?: string) => {
  const params = new URLSearchParams();
  if (cursor) params.set('cursor', cursor);
  if (effectiveAt) params.set('effective_at', effectiveAt);
  const query = params.toString();
  return apiClient.get<CatalogPage<PortfolioMembership>>(`/v1/catalog/portfolios/${encodeURIComponent(portfolioId)}/memberships${query ? `?${query}` : ''}`);
};

export const addPortfolioMembership = (
  portfolioRef: RecordReference, productRef: RecordReference, validFrom: string, receipt = productReceipt(),
) => apiClient.request<CatalogMutationReceipt>(`/v1/catalog/portfolios/${encodeURIComponent(portfolioRef.record_id)}/memberships`, {
  method: 'POST', headers: { 'Idempotency-Key': receipt },
  body: { portfolio_ref: portfolioRef, product_ref: productRef, source_refs: [portfolioRef, productRef], valid_from: validFrom },
});

export const closePortfolioMembership = (
  portfolioId: string, membership: PortfolioMembership, validTo: string, receipt = productReceipt(),
) => apiClient.request<CatalogMutationReceipt>(`/v1/catalog/portfolios/${encodeURIComponent(portfolioId)}/memberships/${encodeURIComponent(membership.relationship_id)}`, {
  method: 'PATCH', headers: { 'Idempotency-Key': receipt, 'If-Match': membership.relationship_revision },
  body: { valid_to: validTo },
});

export interface FinancialAllocation {
  id: string; name: string; target_ref: RecordReference;
  amount: { amount: string; currency: string }; category: 'ai' | 'cloud' | 'other';
  period_start: string; period_end: string; approval_status: 'proposed';
  owner_ref: { kind: 'user'; id: string };
}

export interface ProductProfile {
  product_id: string;
  project_id: string;
  revision: number;
  delivery_home_repository?: { provider: 'github'; repository: string; repository_id: number } | null;
  delivery_home_implied?: boolean;
  progress?: { completed: number; applicable: number; percentage: number; maturity: string };
  peer_availability?: Record<'context' | 'l5', { status: 'unavailable'; reason: 'integration_deferred'; message: string }>;
  project?: { id: string; name: string; owner_user_id: string };
  tenant_id: string;
  configuration: { financials?: { allocations?: FinancialAllocation[]; initiatives?: RecordReference[]; release_target_date?: string } };
  provenance?: { state: string; method: string; paths: string[]; source_refs: RecordReference[]; observed_at?: string; stale_after?: string }[];
  product: {
    name: string;
    purpose: string;
    intended_users: string[];
    owner_ref: { kind: string; id: string };
    team_id?: string;
    lifecycle_status?: string;
    starting_point?: 'idea' | 'existing_assets' | 'running_product';
  };
}

export interface ProductSummary {
  id: string;
  name: string;
  revision: number;
  project_ids: string[];
  projects?: { id: string; name: string; is_base?: boolean }[];
  base_project_id?: string | null;
  lifecycle_status?: string;
  team?: { id: string; name: string };
}

export interface ProductPerson { id: string; name: string; github_username: string | null }
export interface ProductPeople {
  product_id: string; revision: number; owner: ProductPerson;
  team: { id: string; name: string } | null; members: ProductPerson[];
  next_offset: number | null; is_owner: boolean;
  permissions: { manage_team: boolean; manage_people: boolean; enroll_github: boolean };
}

export const getProductPeople = (productId: string, projectId: string, offset = 0) =>
  apiClient.get<ProductPeople>(`/v1${productPath(productId, projectId)}/people?offset=${offset}`);

export const updateProductPeople = (productId: string, projectId: string, revision: number,
  patch: { team_id?: string | null; owner_user_id?: string }, receipt = productReceipt()) =>
  apiClient.request<ProductProfile>(`/v1${productPath(productId, projectId)}`, {
    method: 'PATCH', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) }, body: patch,
  });

export interface ProductPage {
  items: ProductSummary[];
  next_cursor: string | null;
}

export const productPath = (productId: string, projectId: string) =>
  `/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}`;

export const listProducts = (cursor?: string) =>
  apiClient.get<ProductPage>(`/v1/products${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);

export async function listProductScopes(productId: string, onNames?: (names: Record<string, string>) => void, onBase?: (id: string | null) => void): Promise<string[]> {
  let cursor: string | undefined;
  do {
    const page = await listProducts(cursor);
    const product = page.items.find((item) => item.id === productId);
    if (product) {
      onBase?.(product.base_project_id ?? null);
      onNames?.(Object.fromEntries((product.projects ?? []).map(scope => [scope.id, scope.name])));
      return product.project_ids;
    }
    cursor = page.next_cursor ?? undefined;
  } while (cursor);
  return [];
}

export interface GovernedProject { id: string; name: string; product_id: string | null; is_base?: boolean }
export interface ProjectPage { items: GovernedProject[]; next_cursor: string | null }

export type ProjectBindingVersion = { valid_to?: string } & (
  | { binding_id: string; project_id: string }
  | { relationship_id: string; to_ref: { record_type: 'project'; record_id: string; project_id: string } }
);
export interface ProjectBindingHistory { items: { relationship_versions: ProjectBindingVersion[] }[] }

export const listProjectBindings = (productId: string, projectId: string) =>
  apiClient.get<ProjectBindingHistory>(`/v1${productPath(productId, projectId)}/project-bindings`);

export const listProjects = (cursor?: string) =>
  apiClient.get<ProjectPage>(`/v1/projects${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);

export const createScope = (productId: string, revision: number, name: string, receipt = productReceipt()) =>
  apiClient.request<ProductProfile>(`/v1/products/${encodeURIComponent(productId)}/projects`, {
    method: 'POST',
    headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
    body: { mode: 'create', name },
  });

export const linkScope = (productId: string, revision: number, projectId: string, receipt = productReceipt()) =>
  apiClient.request<ProductProfile>(`/v1/products/${encodeURIComponent(productId)}/projects`, {
    method: 'POST',
    headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
    body: { mode: 'select', project_id: projectId },
  });

export const unlinkScope = (productId: string, revision: number, projectId: string, receipt = productReceipt()) =>
  apiClient.request<ProductProfile>(`/v1${productPath(productId, projectId)}`, {
    method: 'DELETE',
    headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
  });

export const getProduct = (productId: string, projectId: string) =>
  apiClient.get<ProductProfile>(`/v1${productPath(productId, projectId)}`);

export const productReceipt = () => crypto.randomUUID().replace(/-/g, '');

export const createProduct = (name: string, purpose: string, intendedUsers: string[], receipt = productReceipt()) =>
  apiClient.request<ProductProfile>('/v1/products', {
    method: 'POST',
    headers: { 'Idempotency-Key': receipt },
    body: { name, purpose, intended_users: intendedUsers, starting_point: 'idea', project: { mode: 'create' } },
  });

export const updateProject = (productId: string, projectId: string, revision: number, name: string, receipt = productReceipt()) =>
  apiClient.request<ProductProfile>(`/v1${productPath(productId, projectId)}/configuration`, {
    method: 'PATCH',
    headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
    body: { name },
  });

export const updateProduct = (
  productId: string,
  projectId: string,
  revision: number,
  fields: { name: string; purpose: string; intended_users: string[] },
  receipt = productReceipt(),
) => apiClient.request<ProductProfile>(`/v1${productPath(productId, projectId)}`, {
  method: 'PATCH',
  headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
  body: fields,
});

export interface OnboardingSession {
  id: string;
  product_id: string;
  project_id: string;
  revision: number;
  state: string;
  steps: Record<'information' | 'basic' | 'advanced' | 'financials', { status: string; reason?: string }>;
  progress: { completed: number; applicable: number; percentage: number; maturity: string };
  proposals?: OnboardingProposal[];
  discoveries?: { id: string; state: string; coverage: string; complete: boolean; resource_count: number;
    cursor?: string; binding_ids?: string[]; adapter?: 'native' | 'devlake' }[];
}

export const startOnboarding = (productId: string, projectId: string, revision: number, receipt = productReceipt()) =>
  apiClient.request<OnboardingSession>(`/v1${productPath(productId, projectId)}/onboarding-sessions`, {
    method: 'POST',
    headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
    body: {},
  });

export const getOnboarding = (productId: string, projectId: string, sessionId: string) =>
  apiClient.get<OnboardingSession>(`/v1${productPath(productId, projectId)}/onboarding-sessions/${encodeURIComponent(sessionId)}`);

export const retryOnboardingDiscovery = (productId: string, projectId: string, sessionId: string, revision: number,
  bindingIds: string[], adapter: 'native' | 'devlake', cursor: string, receipt: string) =>
  apiClient.request<{ id: string; state: string; coverage: string }>(
    `/v1${productPath(productId, projectId)}/onboarding-sessions/${encodeURIComponent(sessionId)}/discoveries`, {
      method: 'POST', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
      body: { binding_ids: bindingIds, adapter, cursor },
    });

export const unlinkOnboardingSource = (productId: string, projectId: string, bindingId: string, revision: number, receipt: string) =>
  apiClient.request<SourceBinding>(`/v1${productPath(productId, projectId)}/bindings/${encodeURIComponent(bindingId)}`, {
    method: 'DELETE', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
  });

export interface RepositoryPlan {
  connection_ref: { kind: 'github_installation'; id: string };
  owner_native_id: string; name: string; visibility: 'private' | 'public' | 'internal';
  template_ref: RecordReference; protections_ref: RecordReference; confirmed: boolean;
}

export const repositoryOptions = (productId: string, projectId: string, sessionId: string, connectionId: string, templateId: string) =>
  apiClient.get<Pick<RepositoryPlan, 'connection_ref' | 'owner_native_id' | 'template_ref' | 'protections_ref'> & { protections: Record<string, unknown> }>(
    `/v1${productPath(productId, projectId)}/onboarding-sessions/${encodeURIComponent(sessionId)}/repository-options?${new URLSearchParams({ connection_id: connectionId, template_id: templateId })}`);

export const previewRepository = (productId: string, projectId: string, sessionId: string, revision: number, plan: RepositoryPlan, receipt: string) =>
  apiClient.request<{ id: string; revision: number; repository_plan: RepositoryPlan }>(
    `/v1${productPath(productId, projectId)}/onboarding-sessions/${encodeURIComponent(sessionId)}/repository-plans`, {
      method: 'POST', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
      body: { repository_plan: plan },
    });

export const confirmRepository = (productId: string, projectId: string, sessionId: string, revision: number,
  previewId: string, plan: RepositoryPlan, receipt: string) =>
  apiClient.request<{ id: string; state: string; repository?: { native_id: string } }>(
    `/v1${productPath(productId, projectId)}/onboarding-sessions/${encodeURIComponent(sessionId)}/handoffs`, {
      method: 'POST', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
      body: { target: 'repository_create', action: 'create_repository', preview_id: previewId,
        repository_plan: { ...plan, confirmed: true } },
    });

export interface OnboardingProposal {
  id: string; base_revision: number; product: Record<string, unknown>; remaining_paths: string[]; expires_at: string;
  configuration?: { financials?: { release_target_date?: string; allocations?: FinancialAllocation[] } };
}
export interface OnboardingGuidance { revision: number; message: string; proposals: OnboardingProposal[]; mode: 'deterministic' | 'model' | 'financial' }

export const guideOnboarding = (
  productId: string, projectId: string, sessionId: string, revision: number, message: string,
  mode: 'deterministic' | 'model' | 'financial' = 'deterministic', receipt = productReceipt(),
) => apiClient.request<OnboardingGuidance>(`/v1${productPath(productId, projectId)}/onboarding-sessions/${encodeURIComponent(sessionId)}/guidance`, {
  method: 'POST', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) }, body: { message, mode },
});

export const listProductChanges = (productId: string, projectId: string) =>
  apiClient.get<{ items: { event_id: string; revision: number }[] }>(`/v1${productPath(productId, projectId)}/changes`);

export const saveFinancialsStep = (
  productId: string, projectId: string, sessionId: string, revision: number,
  financials: NonNullable<ProductProfile['configuration']['financials']>, receipt = productReceipt(),
) => apiClient.request<OnboardingSession>(`/v1${productPath(productId, projectId)}/onboarding-sessions/${encodeURIComponent(sessionId)}/steps/financials`, {
  method: 'PUT',
  headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
  body: { action: 'save', configuration: { financials } },
});

export const confirmFinancialDate = (
  productId: string, projectId: string, sessionId: string, revision: number, proposalId: string, receipt: string,
) => apiClient.request<OnboardingSession>(`/v1${productPath(productId, projectId)}/onboarding-sessions/${encodeURIComponent(sessionId)}/steps/financials`, {
  method: 'PUT', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
  body: { action: 'confirm', proposal_id: proposalId, confirmed_paths: ['/configuration/financials/release_target_date'] },
});

export const setFinancialsApplicabilityStep = (
  productId: string, projectId: string, sessionId: string, revision: number,
  action: 'later' | 'not_applicable', reason: string, receipt = productReceipt(),
) => apiClient.request<OnboardingSession>(`/v1${productPath(productId, projectId)}/onboarding-sessions/${encodeURIComponent(sessionId)}/steps/financials`, {
  method: 'PUT',
  headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
  body: { action, reason },
});

export interface SourceResource {
  kind: 'repository' | 'tracker' | 'document' | 'environment' | 'pipeline' | 'ci_job' | 'support' | 'quality_project' | 'deployment_application' | 'incident_service' | 'tracker_team' | 'communication_channel';
  namespace: string; native_id: string; name: string; source_revision: string;
  full_name?: string;
  repository_id?: string;
  verified_environment?: { account_id: string; regions: string[]; role_ref: string };
}
export interface ProductSource {
  connection_ref: { kind: 'github_installation' | 'credential'; id: string };
  provider: string; label: string; health: string;
  coverage: 'complete' | 'partial' | 'unavailable' | 'denied';
  capabilities: string[]; resources?: SourceResource[]; resource_cursor?: string;
}
export interface SourceBinding {
  id: string; project_id: string; status: string;
  selection?: { kind: string; provider: string; namespace: string; native_id: string; connection_ref?: ProductSource['connection_ref'] };
}
export const listProductSources = (productId: string, projectId: string, cursor?: string) =>
  apiClient.get<{ items: ProductSource[]; next_cursor: string | null }>(
    `/v1${productPath(productId, projectId)}/sources${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);

export const inspectProductSource = (productId: string, projectId: string, connection: ProductSource['connection_ref'], resourceCursor?: string) =>
  apiClient.get<{ items: ProductSource[] }>(`/v1${productPath(productId, projectId)}/sources?${new URLSearchParams({
    connection_kind: connection.kind, connection_id: connection.id, ...(resourceCursor ? { resource_cursor: resourceCursor } : {}),
  }).toString()}`);

export const inspectProductPipelines = (productId: string, projectId: string, connection: ProductSource['connection_ref'], repositoryId: string, resourceCursor?: string) =>
  apiClient.get<{ items: ProductSource[] }>(`/v1${productPath(productId, projectId)}/sources?${new URLSearchParams({
    connection_kind: connection.kind, connection_id: connection.id, pipeline_repository_id: repositoryId,
    ...(resourceCursor ? { resource_cursor: resourceCursor } : {}),
  }).toString()}`);

export const listSourceBindings = (productId: string, projectId: string) =>
  apiClient.get<{ items: SourceBinding[] }>(`/v1${productPath(productId, projectId)}/bindings`);

export const verifySourceBinding = (productId: string, projectId: string, bindingId: string, revision: number, receipt = productReceipt()) =>
  apiClient.request<{ operation_id: string; status: string; coverage: string | null }>(
    `/v1${productPath(productId, projectId)}/bindings/${encodeURIComponent(bindingId)}/verify`, {
      method: 'POST', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) },
    });

export const bindProductSource = (productId: string, projectId: string, revision: number, selection: {
  kind: SourceResource['kind']; provider: string; namespace: string; native_id: string;
  connection_ref: ProductSource['connection_ref']; environment?: { name: string; environment_class: string; production: boolean };
  repository_id?: string; source_revision?: string; display_name?: string;
}, receipt = productReceipt()) => apiClient.request<SourceBinding>(`/v1${productPath(productId, projectId)}/bindings`, {
  method: 'POST', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) }, body: selection,
});

export interface ProjectBudget {
  product_id: string; project_id: string; project_name: string; is_base: boolean;
  budget_usd: string | number | null; spent_usd: string | number | null;
  reserved_usd: string | number | null; available_usd: string | number | null;
  accounting_status: 'current' | 'pending' | 'unavailable'; can_manage: boolean;
  flows: { flow_id: string | null; flow_name?: string | null; spent_usd: string | number; run_count: number }[];
}
export const getProjectBudget = (projectId: string) => apiClient.get<ProjectBudget>(`/v1/projects/${encodeURIComponent(projectId)}/budget`);
export const setProjectBudget = (projectId: string, budget: string) => apiClient.request<ProjectBudget>(`/v1/projects/${encodeURIComponent(projectId)}/budget`, {
  method: 'PUT', body: { budget_usd: budget },
});
export const assignChatProject = (sessionId: string, productId: string, projectId?: string) =>
  apiClient.request(`/chat/sessions/${encodeURIComponent(sessionId)}/project`, { method: 'PUT', body: { product_id: productId, ...(projectId ? { project_id: projectId } : {}) } });

export type ProfileSection = 'repositories' | 'environments' | 'backlog' | 'knowledge';
export interface ProfileResource {
  id: string; kind: string; provider: string; label: string; namespace: string; native_id: string;
  purpose?: 'product_code' | 'reference'; default?: boolean;
  bindings: { id: string; project_id: string; revision: number; generation: number; relationship_revision: number; status: string }[];
}
export interface ProfileEnvironment {
  id: string; name: string; environment_class: 'development' | 'test' | 'staging' | 'production' | 'other';
  resource_id: string; deployment_target: string;
}
export interface SharedProductProfile {
  product_id: string; revision: number; base_project_id: string | null; can_edit: boolean;
  resources: ProfileResource[]; environments: ProfileEnvironment[]; not_applicable: ProfileSection[]; knowledge_count: number | null;
  completion: { completed: number; applicable: number; percentage: number;
    sections: { id: 'details' | ProfileSection; status: 'complete' | 'missing' | 'not_applicable' | 'unavailable' }[] };
}
export interface SharedProfilePatch {
  repositories?: Record<string, { purpose: 'product_code' | 'reference'; label?: string }>;
  default_repository?: string | null;
  environments?: Record<string, Omit<ProfileEnvironment, 'id'> | null>;
  not_applicable?: ProfileSection[];
}
export const getSharedProductProfile = (productId: string) =>
  apiClient.get<SharedProductProfile>(`/v1/products/${encodeURIComponent(productId)}/profile`);
export const updateSharedProductProfile = (productId: string, revision: number, patch: SharedProfilePatch, receipt = productReceipt()) =>
  apiClient.request<ProductProfile>(`/v1/products/${encodeURIComponent(productId)}/profile`, {
    method: 'PATCH', headers: { 'Idempotency-Key': receipt, 'If-Profile-Revision': String(revision) }, body: { profile_configuration: patch },
  });
