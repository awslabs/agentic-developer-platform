/**
 * Rate Limit MSW Handlers
 *
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Mock handlers for rate limit management API endpoints.
 */

import { http, HttpResponse } from 'msw';

const mockRateLimits = [
  {
    org_id: 'org-001',
    entity_type: 'org',
    entity_id: 'org-001',
    rpm: 1000,
    tpm: 100000,
    concurrent_requests: 10,
    updated_at: new Date().toISOString(),
  },
  {
    org_id: 'org-001',
    entity_type: 'department',
    entity_id: 'dept-001',
    rpm: 500,
    tpm: 50000,
    concurrent_requests: 5,
    updated_at: new Date().toISOString(),
  },
  {
    org_id: 'org-001',
    entity_type: 'team',
    entity_id: 'team-001',
    rpm: 100,
    tpm: 10000,
    concurrent_requests: 3,
    updated_at: new Date().toISOString(),
  },
];

export const ratelimitHandlers = [
  // List rate limits for an organization
  http.get('/api/admin/organizations/:orgId/ratelimits', ({ params, request }) => {
    const url = new URL(request.url);
    const entityType = url.searchParams.get('entity_type');
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('limit') || url.searchParams.get('page_size') || '20');

    let ratelimits = mockRateLimits.filter((r) => r.org_id === params.orgId);
    if (entityType) {
      ratelimits = ratelimits.filter((r) => r.entity_type === entityType);
    }

    const start = (page - 1) * pageSize;
    const items = ratelimits.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: ratelimits.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < ratelimits.length,
    });
  }),

  // Get single rate limit config
  http.get('/api/admin/organizations/:orgId/ratelimit/:entityType/:entityId', ({ params }) => {
    const ratelimit = mockRateLimits.find(
      (r) =>
        r.org_id === params.orgId &&
        r.entity_type === params.entityType &&
        r.entity_id === params.entityId
    );

    if (!ratelimit) {
      return HttpResponse.json(null);
    }

    return HttpResponse.json(ratelimit);
  }),

  // Create rate limit config
  http.post('/api/admin/organizations/:orgId/ratelimits', async ({ params, request }) => {
    const body = (await request.json()) as {
      entity_type: string;
      entity_id: string;
      rpm?: number | null;
      tpm?: number | null;
      concurrent_requests?: number | null;
    };

    const newRateLimit = {
      org_id: params.orgId as string,
      entity_type: body.entity_type,
      entity_id: body.entity_id,
      rpm: body.rpm ?? null,
      tpm: body.tpm ?? null,
      concurrent_requests: body.concurrent_requests ?? null,
      updated_at: new Date().toISOString(),
    };

    return HttpResponse.json(newRateLimit, { status: 201 });
  }),

  // Update rate limit config
  http.put('/api/admin/organizations/:orgId/ratelimit/:entityType/:entityId', async ({ params, request }) => {
    const body = (await request.json()) as {
      rpm?: number | null;
      tpm?: number | null;
      concurrent_requests?: number | null;
    };

    const existingRatelimit = mockRateLimits.find(
      (r) =>
        r.org_id === params.orgId &&
        r.entity_type === params.entityType &&
        r.entity_id === params.entityId
    );

    const updatedRateLimit = existingRatelimit
      ? { ...existingRatelimit, ...body, updated_at: new Date().toISOString() }
      : {
          org_id: params.orgId as string,
          entity_type: params.entityType as string,
          entity_id: params.entityId as string,
          rpm: body.rpm ?? null,
          tpm: body.tpm ?? null,
          concurrent_requests: body.concurrent_requests ?? null,
          updated_at: new Date().toISOString(),
        };

    return HttpResponse.json(updatedRateLimit);
  }),

  // Delete rate limit config
  http.delete('/api/admin/organizations/:orgId/ratelimit/:entityType/:entityId', () => {
    return HttpResponse.json({ success: true });
  }),
];
