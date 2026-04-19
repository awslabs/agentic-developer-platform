/**
 * TokenEstimator port — estimates token count from text.
 *
 * Implementations: CharBasedEstimator
 */
export interface TokenEstimator {
  count(text: string): number;
}
