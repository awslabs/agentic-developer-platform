/**
 * NextUnavailable — Issue #5079 (NUI-01 of EPIC #5078).
 *
 * Fallback rendered when the /next subtree fails: a lazy chunk that 404s after a
 * deploy, a runtime error inside a new-UI page, anything the boundary catches.
 *
 * The acceptance criterion is that a new-route or chunk failure still leaves a
 * working way into the current UI. Two details make that true:
 *
 * - The escape hatch is a plain `<a href="/">`, NOT a router `Link`. A failed
 *   chunk means part of the new-UI module graph is missing, so a client-side
 *   navigation could re-enter the same broken subtree or hit the same stale asset
 *   manifest. A full document load fetches the current index.html and renders the
 *   current UI from scratch.
 * - It renders standalone. It does not depend on `NextLayout` having mounted,
 *   because the boundary sits OUTSIDE the layout in the route tree — otherwise a
 *   layout-level failure would leave the user with no visible way back.
 *
 * The copy is deliberately neutral about consequences. An earlier revision
 * promised that nothing in the user's account, settings or data had changed. This
 * boundary also catches errors thrown by a preview PAGE, which may be mid-action,
 * so that guarantee is not one this component can make unconditionally — and a
 * reassurance that turns out to be false is worse than none. It states what
 * happened and where to go instead.
 *
 * This is a UI fallback, not an error reporter: nothing here is authorization or
 * data related, and no state is cleared.
 */

export function NextUnavailable() {
  return (
    <div
      className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 p-4"
      data-testid="next-unavailable"
    >
      <div className="max-w-md w-full bg-white dark:bg-gray-800 rounded-lg shadow-lg p-6 text-center">
        <h1 className="text-xl font-bold text-gray-900 dark:text-white mb-2">
          The new UI preview could not load
        </h1>
        <p className="text-gray-600 dark:text-gray-400 mb-6">
          Return to the current UI to continue. You can try the preview again later.
        </p>
        {/* Full document load, not a client-side navigation — see the file
            comment. */}
        <a
          href="/"
          data-testid="next-unavailable-current-ui"
          className="inline-block px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 transition-colors"
        >
          Go to the current UI
        </a>
      </div>
    </div>
  );
}
