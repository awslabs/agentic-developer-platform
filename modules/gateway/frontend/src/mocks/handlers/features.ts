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
      },
    });
  }),
];
