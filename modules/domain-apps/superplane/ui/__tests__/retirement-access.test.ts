import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

import {
  ScopeGuard,
  parseRetirementAccessReview,
  parseRetirementReview,
  previewRetirementAccess,
} from '@superplane-ui/client';
import { ENDPOINTS } from '@superplane-ui/contract';

const apiRoot = join(process.cwd(), '..', '..', 'domain-apps', 'superplane', 'src', 'superplane-api', 'app');
const source = (path: string) => readFileSync(join(apiRoot, path), 'utf8');
const routeEntries = JSON.parse(readFileSync(join(process.cwd(), '..', 'src', 'domain_proxy', 'superplane_routes.json'), 'utf8')) as [string, string][];

const requestId = '723ee352-087b-4935-92d2-9abeb4f36512';
const accessRequestId = 'a4f7085a-e999-4397-a72a-09810d3815e9';
const allocationId = 'dfd3e34f-fd3f-4bf7-bd2c-8a883f3a783d';
const originalAllocationId = 'f8562d6d-3d26-497d-9c2a-f3ebcab07402';
const workspaceId = '2afed00b-5089-4849-a9d0-1fc54647f251';
const revision = 'a'.repeat(64);

function accessReview() {
  return {
    retirement_request_id: requestId,
    request_id: accessRequestId,
    workspace_id: workspaceId,
    source_operation_id: 'original-provision-operation',
    phase: 'prepare-retirement-access',
    revision,
    allocation_id: allocationId,
    original_allocation_id: originalAllocationId,
    inventory_sha256: 'b'.repeat(64),
    access_plan: {
      request_id: accessRequestId,
      retirement_request_id: requestId,
      workspace_id: workspaceId,
      allocation_id: allocationId,
      original_allocation_id: originalAllocationId,
    },
    authority: { registrar: { policy: 'AmazonEKSAdminPolicy' }, cleaner: {} },
    preserved: ['unowned-resource'],
    max_resource_units: 0,
    max_cost_micros: 0,
    approval_request: {
      workspace_id: workspaceId,
      action: 'provision',
      idempotency_key: accessRequestId,
      parameters: { retirement_request_id: requestId },
    },
  };
}

describe('retirement access producer boundary', () => {
  it('tracks the exact dormant router, request fields and source preview rather than advertising access prematurely', () => {
    const api = source('main.py');
    const router = source('routers/retirement_access.py');
    const service = source('services/retirement_access.py');
    const inventory = source('endpoint_inventory.py');
    for (const [name, path] of [
      ['previewRetirementAccess', '/workspaces/{workspace_id}/retirement/access/preview'],
      ['admitRetirementAccess', '/workspaces/{workspace_id}/retirement/access'],
    ] as const) {
      expect(ENDPOINTS[name].path).toBe(path);
      expect(router).toContain(`@router.post("${path}")`);
      const mounted = api.includes('app.include_router(retirement_access_router)');
      const allowlisted = routeEntries.some(([method, route]) => method === 'POST' && route === path);
      const inventoried = inventory.includes(`("POST", "${path}")`);
      expect(ENDPOINTS[name].served).toBe(mounted && allowlisted && inventoried);
      expect(ENDPOINTS[name].served).toBe(false);
    }
    expect(router).toContain('body.operation_id');
    expect(router).toContain('body.plan_revision');
    expect(router).toContain('body.approval_id');
    for (const field of ['retirement_request_id', 'request_id', 'workspace_id', 'source_operation_id',
      'allocation_id', 'original_allocation_id', 'inventory_sha256', 'access_plan',
      'authority', 'preserved', 'max_resource_units', 'max_cost_micros', 'approval_request']) {
      expect(service).toContain(`"${field}":`);
    }
  });

  it('never sends access review requests until all three serving boundaries are present', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    try {
      const result = await previewRetirementAccess(new ScopeGuard(), workspaceId, { operation_id: requestId });
      expect(result).toMatchObject({ ok: false, unavailable: { reason: 'not-deployed', endpoint: 'previewRetirementAccess' } });
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('binds the separate allocation, original request and approval to one source-shaped access review', () => {
    expect(parseRetirementAccessReview(accessReview())).toMatchObject({
      retirement_request_id: requestId,
      request_id: accessRequestId,
      allocation_id: allocationId,
      original_allocation_id: originalAllocationId,
    });
    const valid = accessReview();
    for (const mutated of [
      { ...valid, revision: null },
      { ...valid, revision: 'changed-plan' },
      { ...valid, access_plan: { ...valid.access_plan, retirement_request_id: 'substituted' } },
      { ...valid, approval_request: { ...valid.approval_request, idempotency_key: requestId } },
      { ...valid, allocation_id: originalAllocationId },
      { ...valid, max_cost_micros: null },
      { ...valid, preserved: [null] },
    ]) {
      expect(parseRetirementAccessReview(mutated)).toBeNull();
    }
  });

  it('keeps the old retirement admission response blocked even with a fabricated approval', () => {
    const service = source('services/retirement.py');
    expect(service).toContain('"admission_available": False');
    expect(service).toContain('"blocked_reason": "staged_cleanup_access_required"');
    expect(service).toContain('raise ProvisioningUnavailable(');
    expect(parseRetirementReview({ ...accessReview(), admission_available: true, approval_request: accessReview().approval_request })).toBeNull();
  });
});
