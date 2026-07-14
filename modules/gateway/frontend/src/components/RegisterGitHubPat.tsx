/**
 * RegisterGitHubPat — form to register a GitHub Personal Access Token in the vault.
 *
 * Issue #3389: PAT onboarding flow (C2 — vault UI).
 * Design note: docs/design-notes/3136-pat-onboarding-flow.md §4.
 *
 * Fixed conventions (not user-editable):
 *   service = "github", credential_type = "bearer", label = "github-pat",
 *   strict = true, scope_hint = "user"
 */

import { useState } from 'react';
import {
  registerGitHubPat,
  extractCredentialError,
} from '@/services/credentials';

interface RegisterGitHubPatProps {
  /** Called after successful registration (parent hides form + reloads list). */
  onSuccess: () => void;
  /** Called when user cancels (parent hides form). */
  onCancel: () => void;
}

export default function RegisterGitHubPat({ onSuccess, onCancel }: RegisterGitHubPatProps) {
  const [patValue, setPatValue] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = patValue.trim().length > 0 && !isSubmitting;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;

    setIsSubmitting(true);
    setError(null);

    try {
      await registerGitHubPat({
        pat: patValue.trim(),
        ...(expiresAt ? { expires_at: expiresAt } : {}),
      });
      // Clear sensitive value from state immediately
      setPatValue('');
      onSuccess();
    } catch (err: unknown) {
      setError(extractCredentialError(err));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="border border-gray-200 rounded-lg p-6 bg-white">
      <h3 className="text-base font-semibold mb-4">Register GitHub Personal Access Token</h3>

      {/* Error display */}
      {error && (
        <div className="mb-4 bg-red-50 border border-red-200 rounded-md p-3">
          <p className="text-sm text-red-800">{error}</p>
        </div>
      )}

      {/* Permission guidance table — design note §4 */}
      <div className="mb-4">
        <p className="text-sm text-gray-700 mb-2">
          Create a{' '}
          <a
            href="https://github.com/settings/personal-access-tokens/new"
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 hover:underline"
          >
            fine-grained PAT
          </a>
          {' '}with these minimum permissions:
        </p>
        <table className="w-full text-sm border border-gray-200 rounded">
          <thead>
            <tr className="bg-gray-50">
              <th className="text-left px-3 py-2 border-b border-gray-200">Permission</th>
              <th className="text-left px-3 py-2 border-b border-gray-200">Access</th>
              <th className="text-left px-3 py-2 border-b border-gray-200">Why</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className="px-3 py-1.5 border-b border-gray-100">Contents</td>
              <td className="px-3 py-1.5 border-b border-gray-100">Read &amp; Write</td>
              <td className="px-3 py-1.5 border-b border-gray-100 text-gray-600">Clone, push, branch creation</td>
            </tr>
            <tr>
              <td className="px-3 py-1.5 border-b border-gray-100">Issues</td>
              <td className="px-3 py-1.5 border-b border-gray-100">Read &amp; Write</td>
              <td className="px-3 py-1.5 border-b border-gray-100 text-gray-600">Read issue body, post comments</td>
            </tr>
            <tr>
              <td className="px-3 py-1.5 border-b border-gray-100">Pull requests</td>
              <td className="px-3 py-1.5 border-b border-gray-100">Read &amp; Write</td>
              <td className="px-3 py-1.5 border-b border-gray-100 text-gray-600">Create PR, request reviewers</td>
            </tr>
            <tr>
              <td className="px-3 py-1.5">Metadata</td>
              <td className="px-3 py-1.5">Read</td>
              <td className="px-3 py-1.5 text-gray-600">Required for all fine-grained PATs</td>
            </tr>
          </tbody>
        </table>
        <p className="text-xs text-gray-500 mt-1">
          Optional: Checks (Read &amp; Write) if the agent posts check-run annotations; Actions (Read) if it monitors workflows.
        </p>
      </div>

      {/* PAT input */}
      <div className="mb-4">
        <label htmlFor="pat-input" className="block text-sm font-medium text-gray-700 mb-1">
          Personal Access Token
        </label>
        <input
          id="pat-input"
          type="password"
          autoComplete="off"
          value={patValue}
          onChange={(e) => setPatValue(e.target.value)}
          placeholder="github_pat_..."
          className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
        />
        <p className="text-xs text-gray-500 mt-1">
          Stored securely in AWS Secrets Manager. Never returned in API responses.
        </p>
      </div>

      {/* Expiry date (optional) */}
      <div className="mb-4">
        <label htmlFor="pat-expiry" className="block text-sm font-medium text-gray-700 mb-1">
          Expiry Date <span className="text-gray-400">(optional)</span>
        </label>
        <input
          id="pat-expiry"
          type="date"
          value={expiresAt}
          onChange={(e) => setExpiresAt(e.target.value)}
          min={new Date().toISOString().split('T')[0]}
          className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
        />
        <p className="text-xs text-gray-500 mt-1">
          Match this to the PAT's expiry in GitHub so the dashboard can warn you before it expires.
        </p>
      </div>

      {/* Self-review notice */}
      <div className="mb-4 bg-yellow-50 border border-yellow-200 rounded-md p-3">
        <p className="text-sm text-yellow-800">
          <strong>Self-review limitation:</strong> PRs created with your PAT are authored by you.
          You cannot approve your own PRs — a teammate must review them.
        </p>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={!canSubmit}
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isSubmitting ? 'Registering...' : 'Register PAT'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 text-sm text-gray-700 hover:text-gray-900"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
