import axios from 'axios';
import { http, HttpResponse } from 'msw';
import { SourceMapConsumer, type RawSourceMap } from 'source-map-js';
import { describe, expect, it } from 'vitest';
import { server } from '../../mocks/server';

describe('dependency security boundaries', () => {
  it('honors disabled redirects with the Axios fetch adapter', async () => {
    let targetRequests = 0;
    server.use(
      http.get('https://redirect.test/start', () => HttpResponse.redirect('https://redirect.test/target')),
      http.get('https://redirect.test/target', () => {
        targetRequests += 1;
        return HttpResponse.text('target response');
      }),
    );
    const blocked = await axios.get('https://redirect.test/start', {
      adapter: 'fetch', maxRedirects: 0, validateStatus: () => true,
    });
    expect(blocked.status).toBe(302);
    expect(targetRequests).toBe(0);

    const allowed = await axios.get('https://redirect.test/start', { adapter: 'fetch' });
    expect(allowed.data).toBe('target response');
    expect(targetRequests).toBe(1);
  });

  it('rejects enormous indexed source-map offsets before processing mappings', () => {
    const sectionMap = { version: 3, sources: ['fixture.js'], names: [], mappings: 'AAAA' };
    // Upstream types describe only basic maps, but the runtime also accepts indexed maps.
    const indexedMap = (line: number) => ({
      version: 3,
      sections: [{ offset: { line, column: 0 }, map: sectionMap }],
    } as unknown as RawSourceMap);
    expect(() => new SourceMapConsumer(indexedMap(2 ** 32))).toThrow(/offset/i);

    const valid = new SourceMapConsumer(indexedMap(1));
    expect(valid.originalPositionFor({ line: 2, column: 1 }).source).toBe('fixture.js');
  });
});
