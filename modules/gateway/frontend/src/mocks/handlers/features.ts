/**
 * MSW handler for GET /api/features — Issue #3566.
 *
 * Returns all-enabled by default so existing tests are unaffected.
 */

import { http, HttpResponse } from 'msw';

export const featuresHandlers = [
  http.get('/api/features', () => {
    return HttpResponse.json({
      features: {
        chat: true,
        knowledge: true,
        indexing: true,
        connections: true,
        credentials: true,
        system_dashboard: true,
        logs: true,
        // Fail-closed add-ons: mocked as the real endpoint ships them (Issues
        // #3773, #4209, #4402), so no test silently exercises an opted-in path.
        gitlab: false,
        orchestration_engine: false,
        budget_spend: false,
      },
    });
  }),
];
