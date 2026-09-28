/**
 * Binds the live session to the journey model — Issue #5080 (NUI-02 of EPIC #5078).
 *
 * The one place that reads hooks and hands their values to the pure model in
 * `components/next/journeys.ts`. Keeping this separate is what lets the whole
 * gating matrix be unit-tested as plain data while the components stay thin.
 *
 * It reuses the SAME `useAuth` and `useFeatures` the current UI reads. There is no
 * second copy of identity, of the active organization, or of the feature flags — so
 * an organization switch or a flag change is one shared change visible in both
 * experiences, and a revoked role takes effect here as soon as the session claims
 * say so rather than being cached into the navigation.
 *
 * The remembered per-journey location is resolved here too, because "is this
 * remembered path still somewhere this actor may go" needs both the storage layer
 * and the live gated model. Validating it here rather than in the storage hook
 * means a permission revoked, or a feature disabled, since the location was stored
 * cannot restore a page the user may no longer see.
 */

import { useCallback, useMemo } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { useFeatures } from '@/hooks/useFeatures';
import { usePermissions } from '@/hooks/usePermissions';
import { useJourneyMemory } from '@/hooks/useJourneyMemory';
import {
  buildJourneys,
  canEnterAdministration,
  isRestorablePath,
  journeyEntries,
  type Journey,
  type JourneyId,
} from '@/components/next/journeys';

export interface UseJourneysResult {
  /** Both journeys, gated for this actor. */
  journeys: Record<JourneyId, Journey>;
  /** The journeys to offer in the switch — Administration only when enterable. */
  available: Journey[];
  /** Whether this actor may enter Administration at all. */
  canAdminister: boolean;
  /**
   * Where the switch should send the actor for `journey`: their remembered
   * location if it is still valid for their current permissions, else the journey
   * home.
   */
  destinationFor: (journey: Journey) => string;
  /** Record `path` as the current location of `journey`. */
  rememberLocation: (journey: JourneyId, path: string) => void;
  /** The id of the entry matching `pathname`, when the current page is one. */
  activeEntryId: (pathname: string) => string | undefined;
}

export function useJourneys(): UseJourneysResult {
  const { user } = useAuth();
  const features = useFeatures();
  const {
    isPlatformAdmin,
    isOrgAdmin,
    isDeptAdmin,
    canViewOrganizations,
    canViewBudgets,
    canViewRateLimits,
    canViewLogs,
    canViewPool,
    canViewMetrics,
  } = usePermissions();

  // Scoped to the active user AND organization, so a switch cannot restore the
  // previous tenant's position.
  const memory = useJourneyMemory(user?.id, user?.orgId);

  const journeys = useMemo(
    () =>
      buildJourneys(features, {
        isPlatformAdmin: isPlatformAdmin(),
        isOrgAdmin: isOrgAdmin(),
        isDeptAdmin: isDeptAdmin(),
        canViewOrganizations: canViewOrganizations(),
        canViewBudgets: canViewBudgets(),
        canViewRateLimits: canViewRateLimits(),
        canViewLogs: canViewLogs(),
        canViewPool: canViewPool(),
        canViewMetrics: canViewMetrics(),
        orgId: user?.orgId,
        deptId: user?.deptId,
      }),
    [
      features,
      isPlatformAdmin,
      isOrgAdmin,
      isDeptAdmin,
      canViewOrganizations,
      canViewBudgets,
      canViewRateLimits,
      canViewLogs,
      canViewPool,
      canViewMetrics,
      user?.orgId,
      user?.deptId,
    ],
  );

  const canAdminister = useMemo(() => canEnterAdministration(journeys), [journeys]);

  const available = useMemo(
    () => (canAdminister ? [journeys.use, journeys.admin] : [journeys.use]),
    [canAdminister, journeys],
  );

  const destinationFor = useCallback(
    (journey: Journey): string => {
      const remembered = memory.remembered(journey.id);
      // Validated against the LIVE model: a location stored while the actor held a
      // role they have since lost is not restorable.
      if (remembered && isRestorablePath(remembered, journeys)) return remembered;
      return journey.home;
    },
    [memory, journeys],
  );

  const rememberLocation = useCallback(
    (journey: JourneyId, path: string) => memory.remember(journey, path),
    [memory],
  );

  const activeEntryId = useCallback(
    (pathname: string): string | undefined => {
      // Matched against the entries this actor can actually see, so a page they
      // reached by URL but may not navigate to does not light up an entry.
      const all = [...journeyEntries(journeys.use), ...journeyEntries(journeys.admin)];
      return all.find((entry) => entry.to === pathname)?.id;
    },
    [journeys],
  );

  return {
    journeys,
    available,
    canAdminister,
    destinationFor,
    rememberLocation,
    activeEntryId,
  };
}
