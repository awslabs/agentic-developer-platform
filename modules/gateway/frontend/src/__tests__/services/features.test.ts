/**
 * Feature-flag service — fail-closed defaults for the engine flag (Issue #4209).
 *
 * `ALL_FEATURES_ENABLED` is not just a constant: `useFeatures` returns it whenever
 * the /features fetch is pending or has failed. So its value for
 * `orchestration_engine` IS the client-side fail-closed behaviour — if it were
 * `true`, every user would see the engine UI flash on during a slow load and stay
 * on it whenever the endpoint was down. That is the consumer-side half of the
 * backend's `_is_enabled_strict`, and it is what these tests pin.
 *
 * The backend/frontend/k8s three-place agreement is asserted in the Python suite
 * (`tests/orchestration/test_feature_flag_parity.py`), which can read all three
 * files; this suite covers the runtime behaviour of the flag module itself.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { apiClient } from '@/services/api';
import { ALL_FEATURES_ENABLED, fetchFeatures } from '@/services/features';
import type { FeatureFlags } from '@/services/features';

vi.mock('@/services/api', () => ({
  apiClient: {
    get: vi.fn(),
  },
}));

describe('features service', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('ALL_FEATURES_ENABLED (the pending/failed-fetch fallback)', () => {
    it('defaults orchestration_engine to false', () => {
      // The load-bearing assertion of this file: an opt-in engine path must be
      // invisible until the server says otherwise.
      expect(ALL_FEATURES_ENABLED.orchestration_engine).toBe(false);
    });

    it('keeps gitlab fail-closed too', () => {
      expect(ALL_FEATURES_ENABLED.gitlab).toBe(false);
    });

    it('still defaults the core modules to enabled', () => {
      // Fail-closed applies to opt-in add-ons only. If this flipped, the whole
      // dashboard would disappear on a slow load.
      expect(ALL_FEATURES_ENABLED.chat).toBe(true);
      expect(ALL_FEATURES_ENABLED.knowledge).toBe(true);
      expect(ALL_FEATURES_ENABLED.indexing).toBe(true);
      expect(ALL_FEATURES_ENABLED.connections).toBe(true);
      expect(ALL_FEATURES_ENABLED.credentials).toBe(true);
      expect(ALL_FEATURES_ENABLED.system_dashboard).toBe(true);
      expect(ALL_FEATURES_ENABLED.logs).toBe(true);
    });

    it('declares every flag the FeatureFlags interface requires', () => {
      // A missing key would be `undefined` at runtime — falsy for a gate, but
      // silently so.
      //
      // `Record<keyof FeatureFlags, true>` rather than the `Array<keyof
      // FeatureFlags>` this used to be: an array of key names is NOT an
      // exhaustiveness check. Every entry typechecks individually and a missing
      // one is simply an array with fewer elements, so a new flag added to the
      // interface never fails this test. `budget_spend` (#4402) had already
      // leaked past it that way before #3960 added a tenth flag. A Record with
      // no index signature must name every key or `tsc` reports the missing
      // one, so the list cannot drift from the interface again.
      const required: Record<keyof FeatureFlags, true> = {
        chat: true,
        knowledge: true,
        indexing: true,
        connections: true,
        credentials: true,
        system_dashboard: true,
        logs: true,
        gitlab: true,
        orchestration_engine: true,
        budget_spend: true,
        agent_control: true,
      };
      for (const key of Object.keys(required) as Array<keyof FeatureFlags>) {
        expect(ALL_FEATURES_ENABLED).toHaveProperty(key);
        expect(typeof ALL_FEATURES_ENABLED[key]).toBe('boolean');
      }
    });

    it('keeps agent_control fail-closed — Issue #3960', () => {
      // The strictest default in the object, and the one whose fail-open cost is
      // highest. `useFeatures` returns ALL_FEATURES_ENABLED while the fetch is in
      // flight AND when it has failed, so `true` here would render pause / steer /
      // abort buttons on every page load before the flags arrive, and keep
      // rendering them through any backend outage — precisely when the control
      // path cannot deliver. An operator who clicks Abort then and sees no error
      // has been told a run stopped when it is still running.
      expect(ALL_FEATURES_ENABLED.agent_control).toBe(false);
    });

    it('keeps budget_spend fail-closed too', () => {
      expect(ALL_FEATURES_ENABLED.budget_spend).toBe(false);
    });
  });

  describe('fetchFeatures', () => {
    it('unwraps the features object from the response', async () => {
      vi.mocked(apiClient.get).mockResolvedValue({
        features: { ...ALL_FEATURES_ENABLED, orchestration_engine: false },
      });

      const flags = await fetchFeatures();

      expect(apiClient.get).toHaveBeenCalledWith('/features');
      expect(flags.orchestration_engine).toBe(false);
    });

    it('passes a server-enabled engine flag through unchanged', async () => {
      // The opt-in has to actually work, or the flag is just a wall.
      vi.mocked(apiClient.get).mockResolvedValue({
        features: { ...ALL_FEATURES_ENABLED, orchestration_engine: true },
      });

      const flags = await fetchFeatures();

      expect(flags.orchestration_engine).toBe(true);
    });

    it('propagates a fetch error rather than resolving to all-enabled', async () => {
      // Swallowing the error here and returning a default would move the
      // fail-open decision out of `useFeatures`, where it is deliberate and
      // visible, into this function, where it would be invisible.
      vi.mocked(apiClient.get).mockRejectedValue(new Error('network down'));

      await expect(fetchFeatures()).rejects.toThrow('network down');
    });
  });
});
