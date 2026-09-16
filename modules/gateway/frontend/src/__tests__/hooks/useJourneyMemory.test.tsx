/**
 * Tests for useJourneyMemory — Issue #5080.
 *
 * The hook exists so switching journeys returns you where you were, and the risk it
 * carries is the opposite of its purpose: a remembered location that survives an
 * organization switch would be leaked prior-tenant state, which the acceptance
 * criteria forbid explicitly.
 *
 * That risk is real *because* the value is persisted. An org switch is a hard
 * `window.location.assign('/')`, so in-memory state is destroyed for free while
 * storage is not — which is why the isolation cases below matter more than the
 * happy path.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useJourneyMemory } from '@/hooks/useJourneyMemory';

function setup(userId: string | undefined, orgId: string | undefined) {
  return renderHook(() => useJourneyMemory(userId, orgId));
}

describe('useJourneyMemory — Issue #5080', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    vi.restoreAllMocks();
  });

  describe('remembering a location per journey', () => {
    it('returns null before anything is recorded', () => {
      const { result } = setup('u-1', 'org-1');
      expect(result.current.remembered('use')).toBeNull();
      expect(result.current.remembered('admin')).toBeNull();
    });

    it('recalls what was recorded for a journey', () => {
      const { result } = setup('u-1', 'org-1');
      act(() => result.current.remember('use', '/next/somewhere'));
      expect(result.current.remembered('use')).toBe('/next/somewhere');
    });

    it('keeps the two journeys’ locations independent', () => {
      // The point of the feature: leaving Administration must not overwrite where
      // you were in Use ADP.
      const { result } = setup('u-1', 'org-1');
      act(() => {
        result.current.remember('use', '/next');
        result.current.remember('admin', '/next/admin');
      });
      expect(result.current.remembered('use')).toBe('/next');
      expect(result.current.remembered('admin')).toBe('/next/admin');
    });

    it('overwrites a journey’s location with the latest one', () => {
      const { result } = setup('u-1', 'org-1');
      act(() => {
        result.current.remember('use', '/next');
        result.current.remember('use', '/next/later');
      });
      expect(result.current.remembered('use')).toBe('/next/later');
    });

    it('survives a remount, which is what makes it survive a refresh', () => {
      const first = setup('u-1', 'org-1');
      act(() => first.result.current.remember('admin', '/next/admin'));
      first.unmount();
      const second = setup('u-1', 'org-1');
      expect(second.result.current.remembered('admin')).toBe('/next/admin');
    });

    it('clears everything for the scope on request', () => {
      const { result } = setup('u-1', 'org-1');
      act(() => {
        result.current.remember('use', '/next');
        result.current.clear();
      });
      expect(result.current.remembered('use')).toBeNull();
    });
  });

  describe('tenant isolation — no prior-organization state leaks', () => {
    it('does not surface another organization’s location', () => {
      // The core criterion. Same user, different org: the position recorded in
      // org-1 must be invisible in org-2 even though it is still on disk when the
      // switch's hard navigation reloads the app.
      const inOrg1 = setup('u-1', 'org-1');
      act(() => inOrg1.result.current.remember('use', '/next/org-one-page'));
      inOrg1.unmount();

      const inOrg2 = setup('u-1', 'org-2');
      expect(inOrg2.result.current.remembered('use')).toBeNull();
    });

    it('does not surface another user’s location', () => {
      // A shared device: signing in as someone else must not reveal where the
      // previous person was, since sessionStorage outlives a logout in the tab.
      const asUser1 = setup('u-1', 'org-1');
      act(() => asUser1.result.current.remember('use', '/next/user-one-page'));
      asUser1.unmount();

      const asUser2 = setup('u-2', 'org-1');
      expect(asUser2.result.current.remembered('use')).toBeNull();
    });

    it('prunes the previous scope’s entry rather than accumulating scopes', () => {
      // Bounded as well as hidden: after writing under org-2 there must be no
      // org-1 record left, so a long session does not build a map of where the
      // person has been in every tenant.
      const inOrg1 = setup('u-1', 'org-1');
      act(() => inOrg1.result.current.remember('use', '/next/org-one-page'));
      inOrg1.unmount();

      const inOrg2 = setup('u-1', 'org-2');
      act(() => inOrg2.result.current.remember('use', '/next/org-two-page'));

      const raw = window.sessionStorage.getItem('adp.next.journeyMemory.v1') ?? '';
      expect(raw).toContain('/next/org-two-page');
      expect(raw).not.toContain('/next/org-one-page');
    });

    it('returning to the original organization does not resurrect its old location', () => {
      // Follows from pruning, and is the case a reviewer would ask about.
      const inOrg1 = setup('u-1', 'org-1');
      act(() => inOrg1.result.current.remember('use', '/next/org-one-page'));
      inOrg1.unmount();

      const inOrg2 = setup('u-1', 'org-2');
      act(() => inOrg2.result.current.remember('use', '/next/org-two-page'));
      inOrg2.unmount();

      const backInOrg1 = setup('u-1', 'org-1');
      expect(backInOrg1.result.current.remembered('use')).toBeNull();
    });

    it('treats a missing organization as its own scope', () => {
      const noOrg = setup('u-1', undefined);
      act(() => noOrg.result.current.remember('use', '/next/no-org'));
      noOrg.unmount();
      expect(setup('u-1', 'org-1').result.current.remembered('use')).toBeNull();
    });
  });

  describe('storage is optional — navigation never depends on it', () => {
    it('degrades to no memory when reading throws', () => {
      // Safari private mode and partitioned contexts throw on access. Losing the
      // convenience is acceptable; throwing inside the layout is not.
      vi.spyOn(window.sessionStorage, 'getItem').mockImplementation(() => {
        throw new Error('storage unavailable');
      });
      const { result } = setup('u-1', 'org-1');
      expect(() => result.current.remembered('use')).not.toThrow();
      expect(result.current.remembered('use')).toBeNull();
    });

    it('degrades quietly when writing throws', () => {
      vi.spyOn(window.sessionStorage, 'setItem').mockImplementation(() => {
        throw new Error('quota exceeded');
      });
      const { result } = setup('u-1', 'org-1');
      expect(() => act(() => result.current.remember('use', '/next'))).not.toThrow();
    });

    it('ignores a malformed stored value', () => {
      // An older build's shape, or a truncated write.
      window.sessionStorage.setItem('adp.next.journeyMemory.v1', 'not json');
      const { result } = setup('u-1', 'org-1');
      expect(result.current.remembered('use')).toBeNull();
    });

    it('ignores a stored value with no scope', () => {
      window.sessionStorage.setItem(
        'adp.next.journeyMemory.v1',
        JSON.stringify({ journeys: { use: '/next/sneaky' } }),
      );
      // Without a matching scope it is not ours to read — an unscoped value could
      // only have come from another tenant's session or a different shape.
      expect(setup('u-1', 'org-1').result.current.remembered('use')).toBeNull();
    });
  });
});
