/**
 * CharBasedEstimator — ~4 chars/token heuristic.
 *
 * Good enough for budget management; exact counts are not needed
 * because the model handles overflow gracefully.
 */
import { TokenEstimator } from './port';

const CHARS_PER_TOKEN = 4;

export class CharBasedEstimator implements TokenEstimator {
  count(text: string): number {
    if (!text) return 0;
    return Math.ceil(text.length / CHARS_PER_TOKEN);
  }
}
