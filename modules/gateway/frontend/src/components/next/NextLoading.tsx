/** Eager fallback: a stalled preview chunk must keep a working escape visible. */
export function NextLoading() {
  return (
    <div className="p-6" data-testid="next-chunk-loading">
      <a href="/" className="text-primary-700 underline dark:text-primary-200">
        Back to current UI
      </a>
      <p role="status" className="mt-4 text-gray-600 dark:text-gray-400">
        Loading the new UI…
      </p>
    </div>
  );
}
