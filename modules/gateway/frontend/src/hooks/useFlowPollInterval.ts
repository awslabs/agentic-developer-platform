import { useState } from 'react';

/** Spread recurring polls without delaying initial reads or the freshness bound. */
export function useFlowPollInterval(maximumMs: number): number {
  const [fraction] = useState(() => 0.9 + Math.random() * 0.1);
  return Math.round(maximumMs * fraction);
}
