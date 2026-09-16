/**
 * Superplane domain app landing page — Issue #5037 (EPIC #4910).
 *
 * Placeholder. This unit creates the module skeleton and its feature gate; the
 * substantive UI arrives with the units that own it. The page exists now so the
 * gated route has a target and the gate itself is testable end to end.
 *
 * It states what is not yet available rather than rendering an empty shell,
 * because the only way to reach it is to deliberately enable a default-off flag
 * in an environment whose backing infrastructure (U3's Terraform, U2's pinned
 * images) may not be deployed yet.
 */

export default function Superplane() {
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">Superplane</h1>
      <p className="text-gray-600 dark:text-gray-400 mb-6">
        The Superplane domain app is enabled in this environment, but its interface has not
        shipped yet.
      </p>
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 p-4">
        <p className="text-sm text-gray-600 dark:text-gray-400">
          This module is being delivered in stages. Workspace management, run monitoring and
          the MLflow integration each arrive with their own release. If you expected a
          working interface here, the environment has the feature flag on ahead of the
          units that populate it.
        </p>
      </div>
    </div>
  );
}
