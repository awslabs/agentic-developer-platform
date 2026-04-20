// The log viewer page previously imported `@/components/logs/LogFilters`,
// `LogTable`, and `Pagination` — none of which exist in the tree. That
// broke the TypeScript build and blocked every frontend deploy. Shipping
// a stub so the rest of the SPA can deploy; the real UI can land when the
// log-viewer components are authored.

export default function LogViewer() {
  return (
    <div className="p-8">
      <h1 className="text-2xl font-semibold mb-4">Log Viewer</h1>
      <p className="text-gray-600">The log viewer is coming soon.</p>
    </div>
  );
}
