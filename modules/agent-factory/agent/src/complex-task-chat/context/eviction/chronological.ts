/**
 * ChronologicalEviction — keep newest, drop oldest.
 *
 * Preserves tool-use/tool-result pairing: if a tool_result is kept,
 * its preceding tool_use is also kept (and vice versa).
 */
import { ResolvedItem } from '../types';
import { EvictionPolicy } from './port';

export class ChronologicalEviction implements EvictionPolicy {
  /**
   * Given evictable items sorted by ordinal (oldest first),
   * return items to KEEP within the token budget.
   * Strategy: keep from newest backward until budget is exhausted.
   */
  pick(evictable: ResolvedItem[], budget: number, _prompt: string): ResolvedItem[] {
    if (evictable.length === 0) return [];

    // Work from newest to oldest
    const reversed = [...evictable].reverse();
    const kept: ResolvedItem[] = [];
    let remaining = budget;

    for (const item of reversed) {
      if (item.tokens <= remaining) {
        kept.push(item);
        remaining -= item.tokens;
      } else {
        // Budget exhausted — stop
        break;
      }
    }

    // Return in chronological order (oldest first)
    return kept.reverse();
  }
}
