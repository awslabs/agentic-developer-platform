/**
 * The flows list query, and the filter state that lives in the URL (#4869).
 *
 * Two decisions here are binding rather than stylistic, and both are ways to
 * silently ship something that looks right:
 *
 * **1. Filters live in the query string, not component state.** The whole point
 * of a "what's stalled" view is being able to send it to someone. Filters held in
 * `useState` produce a URL that is the same whatever the operator has selected, so
 * the link they paste into Slack opens the unfiltered list and the colleague sees
 * a different page from the one described. `useSearchParams` makes the URL the
 * single source of truth, so back/forward and reload also behave.
 *
 * **2. Placeholder data is retained only within the same Product scope.** Filter
 * changes keep the current list while loading, but switching Products must not
 * temporarily display another Product's flows.
 *
 * The 30s `refetchInterval` is explicit for the reason `useFlowGraph` documents:
 * the app sets a global `staleTime` of five minutes, so a query relying on cache
 * invalidation shows five-minute-old state on a page whose purpose is answering
 * "what needs me right now" — and it looks live, because the data is real, just
 * old.
 */

import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { listFlows } from '@/services/orchestration';
import type { FlowList, FlowListParams, FlowStatus } from '@/types/orchestration';
import { useFlowPollInterval } from '@/hooks/useFlowPollInterval';

/**
 * Poll cadence, matching `useFlowGraph`. The two views poll the same engine, and
 * a list that refreshes on a different beat from the detail it links to makes the
 * two disagree for no reason a user can see.
 */
export const FLOWS_REFETCH_INTERVAL_MS = 30_000;

/** Page size. Well under the route's cap of 100, which is a 422 rather than a clamp. */
export const FLOWS_PAGE_SIZE = 25;

/** The sort keys the route accepts. No `cost` — see `FlowListParams`. */
export const FLOW_SORTS = ['created', 'updated', 'stalled'] as const;
export type FlowSort = (typeof FLOW_SORTS)[number];

/** The seven statuses, in the order the chips render. Archived work is last. */
export const FLOW_STATUSES: readonly FlowStatus[] = [
  'attention_needed',
  'awaiting_you',
  'running',
  'queued',
  'complete',
  'empty',
  'archived',
];

const FLOW_STATUS_SET = new Set<string>(FLOW_STATUSES);

/**
 * The filters as read from the URL, already validated.
 *
 * Validated because the URL is user-editable: a hand-typed `?status=on_fire` or
 * `?sort=cost` would otherwise be forwarded to the API, come back 422, and render
 * as "this page is broken" rather than as the unfiltered list the operator can
 * still use. Unrecognised values are dropped, not passed through.
 */
export interface FlowFilters {
  q: string;
  status: FlowStatus | undefined;
  needsMe: boolean;
  includeArchived: boolean;
  sort: FlowSort;
  offset: number;
}

function parseOffset(raw: string | null): number {
  const parsed = Number(raw);
  // `Number('')` is 0 and `Number(null)` is 0, so the guard is on finiteness and
  // sign rather than on absence. A negative offset is a 422 server-side.
  if (!Number.isFinite(parsed) || parsed < 0) return 0;
  return Math.floor(parsed);
}

/**
 * Read the filters out of the URL, plus a setter that writes them back.
 *
 * The setter takes a partial update and **resets `offset` to 0** unless the
 * update names it: changing a filter while on page 3 of the old result set lands
 * the operator on a page that may not exist in the new one, which renders as an
 * empty list and reads as "no matches" rather than "you are past the end".
 */
export function useFlowFilters(): {
  filters: FlowFilters;
  setFilters: (update: Partial<FlowFilters>) => void;
} {
  const [searchParams, setSearchParams] = useSearchParams();

  const filters = useMemo<FlowFilters>(() => {
    const rawStatus = searchParams.get('status');
    const rawSort = searchParams.get('sort');
    return {
      q: searchParams.get('q') ?? '',
      status: rawStatus && FLOW_STATUS_SET.has(rawStatus) ? (rawStatus as FlowStatus) : undefined,
      needsMe: searchParams.get('needs_me') === 'true',
      includeArchived: searchParams.get('include_archived') === 'true',
      sort: (FLOW_SORTS as readonly string[]).includes(rawSort ?? '') ? (rawSort as FlowSort) : 'created',
      offset: parseOffset(searchParams.get('offset')),
    };
  }, [searchParams]);

  const setFilters = useCallback(
    (update: Partial<FlowFilters>) => {
      const next = { ...filters, ...update };
      // Any filter change returns to the first page unless the caller is the pager.
      const offset = 'offset' in update ? next.offset : 0;

      const params = new URLSearchParams();
      // Defaults are omitted rather than written out, so a shared link carries
      // only the filters actually applied and an unfiltered page has a clean URL.
      if (next.q) params.set('q', next.q);
      if (next.status) params.set('status', next.status);
      if (next.needsMe) params.set('needs_me', 'true');
      if (next.includeArchived) params.set('include_archived', 'true');
      if (next.sort !== 'created') params.set('sort', next.sort);
      if (offset > 0) params.set('offset', String(offset));

      // `replace` so a filter keystroke does not push a history entry per
      // character — Back should leave the page, not walk the search box.
      setSearchParams(params, { replace: true });
    },
    [filters, setSearchParams]
  );

  return { filters, setFilters };
}

/** Map validated URL filters onto the wire params. */
export function toFlowListParams(filters: FlowFilters): FlowListParams {
  return {
    limit: FLOWS_PAGE_SIZE,
    offset: filters.offset,
    q: filters.q || undefined,
    status: filters.status,
    needs_me: filters.needsMe,
    include_archived: filters.includeArchived,
    sort: filters.sort,
  };
}

export function useFlows(filters: FlowFilters, productId?: string, projectId?: string, readerKey?: string) {
  const params = { ...toFlowListParams(filters), ...(productId ? { product_id: productId } : {}), ...(projectId ? { project_id: projectId } : {}) };
  const interval = useFlowPollInterval(FLOWS_REFETCH_INTERVAL_MS);

  return useQuery<FlowList>({
    // Every filter is in the key: two different filter sets are two different
    // results, and sharing a key would serve one operator's search to the next.
    queryKey: ['orchestration', 'flows', params, readerKey],
    queryFn: ({ signal }) => listFlows(params, signal),
    refetchInterval: interval,
    ...(projectId ? { staleTime: 0, gcTime: 0 } : {}),
    refetchIntervalInBackground: false,
    // An operator who tabs away to GitHub and back expects current state on
    // arrival, not on the next tick. `'always'` refetches even while fresh —
    // plain `true` defers to the global `staleTime` and would do nothing.
    refetchOnWindowFocus: 'always',
    // Keep pagination/filter changes smooth, but never show another Product's
    // previous page while the new Product's request is pending or denied.
    placeholderData: (previous, query) =>
      (query?.queryKey[2] as FlowListParams | undefined)?.product_id === productId && (query?.queryKey[2] as FlowListParams | undefined)?.project_id === projectId && query?.queryKey[3] === readerKey ? previous : undefined,
  });
}
