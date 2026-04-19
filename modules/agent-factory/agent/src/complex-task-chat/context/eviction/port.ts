/**
 * EvictionPolicy port — selects which context items to keep within a budget.
 *
 * Implementations: ChronologicalEviction
 */
import { ResolvedItem } from '../types';

export interface EvictionPolicy {
  /**
   * Given evictable items, a token budget, and the current user prompt,
   * return the items to KEEP (within budget).
   */
  pick(evictable: ResolvedItem[], budget: number, prompt: string): ResolvedItem[];
}
