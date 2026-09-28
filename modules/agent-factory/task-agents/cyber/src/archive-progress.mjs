/** Publish only confirmed archive observations; unchanged polls get a paced status reminder. */
export function archiveProgress(bridge, now = Date.now) {
  const scans = new Map();
  return (receipt, scanId, exhausted = false) => {
    if (bridge.controller.signal.aborted || receipt.operation_status !== 'confirmed' || !scanId) return;
    const result = receipt.result;
    if (!result) return;
    const state = result.query_state;
    const status = result.status;
    let key, message;
    if (status === 'completed') {
      key = 'completed';
      if (Array.isArray(result.captures)) {
        const count = result.captures.length;
        message = count ? `Common Crawl returned ${count} archived capture${count === 1 ? '' : 's'} for review.`
          : 'Common Crawl returned no matching captures in the searched archives. This does not establish that the site is safe.';
      } else message = 'Common Crawl archive search completed.';
    } else if (state === 'CANCELLED' || status === 'cancelled') {
      key = 'cancelled'; message = 'Common Crawl archive search was cancelled; historical coverage is incomplete.';
    } else if (status === 'failed') {
      key = 'failed'; message = result.reason === 'query_deadline_exceeded'
        ? 'Common Crawl archive search reached its time limit; historical coverage is incomplete.'
        : 'Common Crawl archive search failed; historical coverage is incomplete.';
    } else if (status === 'partial') {
      key = 'partial'; message = 'Common Crawl could not provide complete archive results; historical coverage is limited.';
    } else if (status === 'pending') {
      key = exhausted ? 'poll-window-ended' : state === 'QUEUED' ? 'queued' : state === 'RUNNING' ? 'running' : 'pending';
      message = exhausted ? 'Common Crawl is still pending after this polling window; archive results are not yet available.'
        : state === 'QUEUED' ? 'Common Crawl archive search is queued.'
        : state === 'RUNNING' ? 'Searching Common Crawl archives for historical captures.'
        : 'Common Crawl archive search submitted; waiting for query status.';
    } else return;
    const time = now();
    const previous = scans.get(scanId);
    const started = previous?.started ?? time;
    if (previous?.key === key) {
      if (status !== 'pending' || exhausted || time - previous.published < 30000) return;
      const seconds = Math.max(0, Math.floor((time - started) / 1000));
      message = state === 'QUEUED' ? `Common Crawl search is still queued (${seconds} seconds elapsed).`
        : state === 'RUNNING' ? `Common Crawl is still searching the archive (${seconds} seconds elapsed).`
        : `Common Crawl results are still pending (${seconds} seconds elapsed).`;
    }
    if (status === 'pending' && Number.isFinite(result.bytes_scanned) && result.bytes_scanned > 0) {
      message += ` Archive data scanned so far: ${(result.bytes_scanned / 1000000).toFixed(1)} MB.`;
    }
    bridge.progress(message, 'analysis');
    scans.set(scanId, {key, started, published: time});
  };
}
