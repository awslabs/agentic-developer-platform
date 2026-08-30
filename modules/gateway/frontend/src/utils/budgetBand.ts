/**
 * Presentation for a server-derived budget band — Issue #4402 (U-5 of EPIC #4324).
 *
 * **This module maps a band to styling. It does not decide the band.** The band
 * arrives on the wire (`BudgetLine.band`), derived server-side from the same
 * `budget_configs` thresholds enforcement itself reads (80% warn, 95% critical,
 * >= 100% exceeded). There is deliberately no threshold constant in this file and no
 * function here takes a percentage, so there is nothing to drift.
 *
 * That is the whole reason it exists. `pages/BudgetManagement.tsx:30-34` derives a
 * band locally from a **different** pair of thresholds (50/80), so the same
 * utilisation is styled one way there and another way by the server. A user at 79%
 * reads "fine" while the server has already crossed its warn line — the
 * screen-vs-enforcer disagreement this EPIC exists to eliminate, in miniature.
 * Fixing that page is out of scope here (#4402 is the new screen); this module is
 * the shape the fix should adopt.
 *
 * `critical` is a **warning, not a stop**: enforcement blocks at >= 100%, so no copy
 * on this path may claim spend has been or will be halted. Whether it is halted at
 * all is `enforcement_mode`'s business, not the band's — under `shadow` the cap is
 * advisory and nothing is blocked at any band.
 */

import type { BudgetBand } from '@/types/budget';

/** How a band renders: a short label, a badge variant, and a text colour. */
export interface BandPresentation {
  /**
   * Short human label. Describes the *position*, never the consequence — "Over cap"
   * rather than "Blocked", because whether anything is blocked depends on
   * `enforcement_mode` and under `shadow` nothing is.
   */
  label: string;
  /** `Badge` variant from `@/components/ui`. */
  variant: 'success' | 'warning' | 'error' | 'default';
  /** Tailwind text colour for a bare figure rendered outside a badge. */
  colorClass: string;
  /** Tailwind background for a progress bar's filled portion. */
  barClass: string;
}

const PRESENTATIONS: Record<BudgetBand, BandPresentation> = {
  none: {
    label: 'Within budget',
    variant: 'success',
    colorClass: 'text-green-700 dark:text-green-400',
    barClass: 'bg-green-500',
  },
  warning: {
    label: 'Approaching cap',
    variant: 'warning',
    colorClass: 'text-amber-700 dark:text-amber-400',
    barClass: 'bg-amber-500',
  },
  critical: {
    label: 'Close to cap',
    variant: 'error',
    colorClass: 'text-orange-700 dark:text-orange-400',
    barClass: 'bg-orange-500',
  },
  exceeded: {
    label: 'Over cap',
    variant: 'error',
    colorClass: 'text-red-700 dark:text-red-400',
    barClass: 'bg-red-500',
  },
};

/** Rendering for a line with no cap at all — not a band, and not a zero-cap line. */
const UNCAPPED: BandPresentation = {
  // "Uncapped", not "No cap set": the row's cap cell already says the latter, and two
  // elements with identical text make the band badge unaddressable in a test and
  // redundant on screen. This label describes the *state*, the cell the *value*.
  label: 'Uncapped',
  variant: 'default',
  colorClass: 'text-gray-500 dark:text-gray-400',
  barClass: 'bg-gray-300 dark:bg-gray-600',
};

/**
 * Presentation for a band as the server reported it.
 *
 * `null` is the **uncapped** line — no budget row governs it, so it has no band and
 * cannot be styled as though it were comfortably within one. Rendering uncapped as
 * `none`/green would claim an all-clear that was never measured; a value the SPA does
 * not recognise degrades to the same neutral styling rather than to a reassuring one.
 */
export function describeBand(band: BudgetBand | null | undefined): BandPresentation {
  if (!band) return UNCAPPED;
  return PRESENTATIONS[band] ?? UNCAPPED;
}

/**
 * Format `utilization_pct` for display.
 *
 * `null` means no percentage is defined — an uncapped line, or a `$0` cap where the
 * ratio has no value. Rendering that as `0%` would read as "plenty of room" when in
 * fact no request with any cost can pass a zero cap, so it renders as an em dash.
 */
export function formatUtilization(pct: number | null | undefined): string {
  if (pct == null || Number.isNaN(pct)) return '—';
  return `${pct.toFixed(1)}%`;
}
