import { CharBasedEstimator } from './char-estimator';

describe('CharBasedEstimator', () => {
  const estimator = new CharBasedEstimator();

  it('estimates empty string as 0', () => {
    expect(estimator.count('')).toBe(0);
  });

  it('estimates ~4 chars per token', () => {
    const text = 'a'.repeat(100);
    expect(estimator.count(text)).toBe(25);
  });

  it('rounds up partial tokens', () => {
    const text = 'a'.repeat(5); // 5/4 = 1.25 -> 2
    expect(estimator.count(text)).toBe(2);
  });

  it('handles null/undefined gracefully', () => {
    expect(estimator.count(null as any)).toBe(0);
    expect(estimator.count(undefined as any)).toBe(0);
  });
});
