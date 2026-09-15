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
        // #3773, #4209, #4402, #3960), so no test silently exercises an opted-in
        // path. agent_control especially: the verbs are all unsupported in S1, so
        // a test that saw it as `true` would be exercising a UI for a control
        // path that answers 501.
        gitlab: false,
        orchestration_engine: false,
        budget_spend: false,
        agent_control: false,
        // Issue #5037: the Superplane domain app. Mocked off for the same reason as
        // the flags above — the infrastructure behind it belongs to later units, so a
        // test seeing `true` would exercise a route with nothing behind it.
        superplane: false,
      },
    });
  }),
];
