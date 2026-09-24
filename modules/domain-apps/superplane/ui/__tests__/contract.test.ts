/**
 * The onboarding contract is checked against the real gateway allowlist — #5730.
 *
 * WHY THIS READS A FILE INSTEAD OF A FIXTURE
 * -----------------------------------------
 * AC-09 asks for "Gateway allowlist and real API boundary tests; no tests that
 * merely repeat the client's own invented paths". A test that compared
 * `contract.ts` against a hand-copied list of routes would pass forever while both
 * drifted away from the proxy, which is exactly the #5637 defect: a client posting
 * to a prefix that looked plausible in the source and 404'd in production.
 *
 * So the authority here is `modules/gateway/src/domain_proxy/superplane_routes.json`
 * — the same file the proxy itself compiles its patterns from, read from disk. If
 * somebody removes a route from the proxy, this test fails. If somebody adds an
 * endpoint to `contract.ts` with `served: true` that the proxy will 404, this test
 * fails. Neither can be made to pass by editing the other side's copy of the truth.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  CREATE_IDEMPOTENCY_FEATURE,
  DOMAIN_BASE,
  ENDPOINTS,
  IDEMPOTENCY_TRANSPORT,
  advertises,
  resolvePath,
  unavailableFor,
  unservedEndpoints,
  type Capabilities,
  type EndpointName,
} from '@superplane-ui/contract';

/**
 * The proxy allowlist, read from the gateway source tree.
 *
 * `process.cwd()` is `modules/gateway/frontend` — vitest sets `root: '.'` there
 * and CI runs with that working directory.
 */
const ALLOWLIST_PATH = join(
  process.cwd(),
  '..',
  'src',
  'domain_proxy',
  'superplane_routes.json',
);

function allowlist(): Set<string> {
  const raw = readFileSync(ALLOWLIST_PATH, 'utf8');
  const pairs = JSON.parse(raw) as [string, string][];
  return new Set(pairs.map(([method, path]) => `${method} ${path}`));
}

describe('gateway allowlist agreement', () => {
  it('finds the proxy allowlist where the proxy itself reads it', () => {
    // Guards the rest of this file: a bad path would make every subset check
    // vacuous, so the fixture's own existence is asserted first.
    const routes = allowlist();
    expect(routes.size).toBeGreaterThan(20);
    expect(routes.has('GET /workspaces')).toBe(true);
  });

  it('every endpoint marked served is actually allowlisted by the proxy', () => {
    const routes = allowlist();
    const claimedButUnserved = (Object.keys(ENDPOINTS) as EndpointName[])
      .filter((name) => ENDPOINTS[name].served)
      .filter((name) => {
        const endpoint = ENDPOINTS[name];
        return !routes.has(`${endpoint.method} ${endpoint.path}`);
      });

    expect(claimedButUnserved).toEqual([]);
  });

  it('no endpoint marked unserved is quietly already available', () => {
    // The inverse direction matters too. If #5535 lands a route and nobody
    // updates `served`, the UI keeps telling users the feature is undeployed
    // while it works. That is a silently degraded product, so it fails here and
    // the failure message says what to flip.
    const routes = allowlist();
    const nowServed = unservedEndpoints().filter((name) => {
      const endpoint = ENDPOINTS[name];
      return routes.has(`${endpoint.method} ${endpoint.path}`);
    });

    expect(nowServed).toEqual([]);
  });

  it('names a user-facing capability for every endpoint it refuses to call', () => {
    // An unserved endpoint with no `capability` produces a diagnostic that says
    // only "not available", which gives the reader nothing to act on.
    for (const name of unservedEndpoints()) {
      expect(ENDPOINTS[name].capability, name).toBeTruthy();
    }
  });

  it('never phrases a capability as a story or ticket reference', () => {
    // The product's remediation must describe the user's system, not our
    // backlog. A story number is unusable to an operator without repository
    // access, and it dates badly once the story closes. Stable technical
    // identifiers live on `reason`/`endpoint`, which automation reads instead.
    for (const name of unservedEndpoints()) {
      const capability = ENDPOINTS[name].capability ?? '';
      expect(capability, name).not.toMatch(/#\d+/);
      expect(capability, name).not.toMatch(/\b(story|ticket|issue|epic|backlog|jira)\b/i);
    }
  });
});

describe('path construction', () => {
  it('prefixes the domain base the gateway mounts', () => {
    expect(resolvePath(ENDPOINTS.listWorkspaces)).toBe(`${DOMAIN_BASE}/workspaces`);
  });

  it('does not include the /api prefix the client adds', () => {
    // Documented gateway gotcha (#4330): `apiClient` prepends `/api`, and a path
    // that includes it produces `/api/api/...`, which hits the SPA fallback and
    // returns HTML with HTTP 200 — a success the caller cannot distinguish.
    for (const name of Object.keys(ENDPOINTS) as EndpointName[]) {
      expect(ENDPOINTS[name].path.startsWith('/api'), name).toBe(false);
    }
    expect(DOMAIN_BASE.startsWith('/api')).toBe(false);
  });

  it('substitutes path parameters', () => {
    expect(
      resolvePath(ENDPOINTS.getConnection, {
        workspace_id: 'ws-1',
        connection_id: 'conn-2',
      }),
    ).toBe(`${DOMAIN_BASE}/workspaces/ws-1/provider-connections/conn-2`);
  });

  it('refuses a missing parameter instead of building a wrong path', () => {
    // Interpolating `undefined` would produce `/workspaces/undefined`, a request
    // for a real-looking workspace that is not the caller's.
    expect(() => resolvePath(ENDPOINTS.getWorkspace, {})).toThrow(/workspace_id/);
    expect(() => resolvePath(ENDPOINTS.getWorkspace, { workspace_id: '' })).toThrow();
  });

  it('encodes a parameter that would otherwise add a path segment', () => {
    // A workspace id of `a/b` must not reach `/workspaces/a/b`, which is a
    // different route. The proxy also rejects raw `%` in the path, so escaping
    // here keeps traversal attempts from resembling a valid nested route.
    const path = resolvePath(ENDPOINTS.getWorkspace, { workspace_id: '../secrets' });
    expect(path).toBe(`${DOMAIN_BASE}/workspaces/..%2Fsecrets`);
    expect(path.split('/').length).toBe(resolvePath(ENDPOINTS.getWorkspace, { workspace_id: 'x' }).split('/').length);
  });
});

describe('idempotency transport', () => {
  it('carries the operation identity in the body, not a header', () => {
    // Pinned as a test because the conventional choice is wrong here and the
    // wrongness is invisible: the proxy forwards only `Authorization` and
    // `Content-Type` (see domain_proxy/superplane.py), so an `Idempotency-Key`
    // header is dropped in transit, the create succeeds, nothing is deduplicated,
    // and a retry builds a second workspace. If someone "modernises" this to a
    // header, this test explains why not.
    expect(IDEMPOTENCY_TRANSPORT).toBe('body');
  });

  it('confirms the proxy forwards no client-chosen headers', () => {
    // The real reason, asserted against the real proxy source rather than trusted
    // from a comment that could go stale.
    const proxy = readFileSync(
      join(process.cwd(), '..', 'src', 'domain_proxy', 'superplane.py'),
      'utf8',
    );
    const headerLine = proxy.match(/headers = \{\s*"Authorization": authorization,[\s\S]*?\n    \}/)?.[0];
    expect(headerLine).toBeTruthy();
    expect(headerLine).toContain('Authorization');
    expect(headerLine).toContain('Content-Type');
    expect(headerLine?.toLowerCase()).not.toContain('idempotency');
  });
});

describe('unavailability reporting', () => {
  it('reports an unserved endpoint as not-deployed, explained by capability', () => {
    const unavailable = unavailableFor('previewWorkspace');
    // The stable pair automation branches on.
    expect(unavailable.reason).toBe('not-deployed');
    expect(unavailable.endpoint).toBe('previewWorkspace');
    // The human explanation leads with what is missing, and cites no story.
    expect(unavailable.capability).toBe(ENDPOINTS.previewWorkspace.capability);
    expect(unavailable.detail).not.toMatch(/#\d+/);
    expect(unavailable.detail).toContain('does not support');
    // The detail must name the actual method and path, so a deployer can check
    // the allowlist rather than guessing which call failed.
    expect(unavailable.detail).toContain('POST');
    expect(unavailable.detail).toContain('/workspaces/preview');
  });

  it('names approval, adopt, preview and operation status as unserved today', () => {
    // A deliberately brittle list. These five are the gap between what the
    // onboarding journeys need and what the baseline serves, and if one of them
    // silently starts or stops being served the UI's honesty depends on noticing.
    expect(unservedEndpoints().sort()).toEqual([
      'adoptWorkspace',
      'continueLifecycleProposal',
      'decideApproval',
      'getApproval',
      'getOperation',
      'listLifecycleProposals',
      'previewLifecycleProposal',
      'previewWorkspace',
      'recoverOperation',
      'requestApproval',
    ]);
  });
});

describe('reading an advertised capability fail-closed', () => {
  const report = (features: string[]): Capabilities => ({
    features,
    modes: ['managed'],
    providers: ['aws'],
    isolationModes: ['dedicated'],
  });

  it('treats no observation at all as no advertisement', () => {
    // The single most important case, and the one a caller would get wrong by
    // reaching for `capabilities?.features.includes(...)`: that yields `undefined`,
    // which is falsy only by luck and flips to a permissive answer the moment
    // somebody writes `if (!supported)`. An unobserved capability is a "no", and
    // this is the assertion that stops it becoming an accidental "yes".
    expect(advertises(null, CREATE_IDEMPOTENCY_FEATURE)).toBe(false);
  });

  it('treats an empty advertisement as no advertisement', () => {
    expect(advertises(report([]), CREATE_IDEMPOTENCY_FEATURE)).toBe(false);
  });

  it('does not accept an unrelated feature as this one', () => {
    // Guards the check against degenerating into "the server advertised something".
    expect(advertises(report(['adopt-v2', 'quota-reads']), CREATE_IDEMPOTENCY_FEATURE)).toBe(
      false,
    );
  });

  it('accepts the feature when it is named', () => {
    expect(
      advertises(report(['adopt-v2', CREATE_IDEMPOTENCY_FEATURE]), CREATE_IDEMPOTENCY_FEATURE),
    ).toBe(true);
  });

  it('does not match a feature name by prefix', () => {
    // `create-operation-id-v1` and `create-operation-id-v10` are different
    // guarantees. A substring match would silently accept a successor revision
    // whose semantics nobody here has read.
    expect(advertises(report(['create-operation-id-v10']), CREATE_IDEMPOTENCY_FEATURE)).toBe(
      false,
    );
  });
});
