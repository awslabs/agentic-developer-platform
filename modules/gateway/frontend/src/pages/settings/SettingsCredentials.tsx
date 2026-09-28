/**
 * Credentials list page — manage vault credentials (AWS roles, API keys, etc.).
 *
 * Issue #562: Self-serve AWS account connect UI.
 * Issue #3389: GitHub PAT vault registration flow.
 *
 * URL: /settings/credentials
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { listCredentials, deleteCredential, extractCredentialError, type CredentialItem } from '@/services/credentials';
import RegisterGitHubPat from '@/components/RegisterGitHubPat';

export default function SettingsCredentials() {
  const [credentials, setCredentials] = useState<CredentialItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [showPatForm, setShowPatForm] = useState(false);
  const [patSuccess, setPatSuccess] = useState(false);

  const loadCredentials = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const creds = await listCredentials();
      setCredentials(creds);
    } catch (err: unknown) {
      setError(extractCredentialError(err));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCredentials();
  }, [loadCredentials]);

  const handleDelete = async (cred: CredentialItem) => {
    const confirmed = window.confirm(
      cred.service === 'aws'
        ? 'This removes ADP\'s record of this AWS role. The IAM role still exists in your AWS account — delete the CloudFormation stack there if you no longer need it.'
        : `Remove credential "${cred.label}"? This cannot be undone.`,
    );
    if (!confirmed) return;

    setDeletingId(cred.id);
    try {
      await deleteCredential(cred.id);
      setCredentials((prev) => prev.filter((c) => c.id !== cred.id));
    } catch (err: unknown) {
      setError(extractCredentialError(err));
    } finally {
      setDeletingId(null);
    }
  };

  const handlePatSuccess = () => {
    setShowPatForm(false);
    setPatSuccess(true);
    loadCredentials();
    // Clear success message after 5 seconds
    setTimeout(() => setPatSuccess(false), 5000);
  };

  // Separate credentials by type
  const awsCredentials = credentials.filter((c) => c.service === 'aws');
  const githubPatCredential = credentials.find(
    (c) => c.service === 'github' && c.label === 'github-pat',
  );
  const otherCredentials = credentials.filter(
    (c) => c.service !== 'aws' && !(c.service === 'github' && c.label === 'github-pat'),
  );

  return (
    <div className="max-w-4xl mx-auto p-6">
      <h1 className="text-2xl font-bold mb-6">My Credentials</h1>

      {error && (
        <div className="mb-4 bg-red-50 border border-red-200 rounded-md p-4">
          <p className="text-sm text-red-800">{error}</p>
        </div>
      )}

      {patSuccess && (
        <div className="mb-4 bg-green-50 border border-green-200 rounded-md p-4">
          <p className="text-sm text-green-800">GitHub PAT registered successfully.</p>
        </div>
      )}

      {isLoading ? (
        <p className="text-gray-500">Loading credentials...</p>
      ) : (
        <>
          {/* AWS Credentials Section */}
          <section className="mb-8">
            <h2 className="text-lg font-semibold mb-3">AWS Accounts</h2>
            {awsCredentials.length === 0 ? (
              <p className="text-sm text-gray-500 mb-3">No AWS accounts connected.</p>
            ) : (
              <div className="space-y-2 mb-3">
                {awsCredentials.map((cred) => (
                  <div
                    key={cred.id}
                    className="flex items-center justify-between p-3 bg-white border border-gray-200 rounded-md"
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-lg">&#x1F517;</span>
                      <div>
                        <span className="font-medium">{cred.label}</span>
                        <span className="text-sm text-gray-500 ml-2">
                          {cred.scopes?.account_id || ''}
                        </span>
                      </div>
                      <span
                        className={`text-xs px-2 py-0.5 rounded ${
                          cred.scopes?.status === 'verified'
                            ? 'bg-green-100 text-green-700'
                            : 'bg-yellow-100 text-yellow-700'
                        }`}
                      >
                        {cred.scopes?.status || 'pending'}
                      </span>
                    </div>
                    <button
                      onClick={() => handleDelete(cred)}
                      disabled={deletingId === cred.id}
                      className="text-sm text-red-600 hover:text-red-800 disabled:opacity-50"
                    >
                      {deletingId === cred.id ? 'Removing...' : 'Remove'}
                    </button>
                  </div>
                ))}
              </div>
            )}
            <Link
              to="/settings/credentials/aws/connect"
              className="inline-flex items-center px-4 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-700"
            >
              + Connect AWS Account
            </Link>
          </section>

          {/* GitHub PAT Section */}
          <section className="mb-8">
            <h2 className="text-lg font-semibold mb-3">GitHub Personal Access Token</h2>
            {githubPatCredential ? (
              <div className="flex items-center justify-between p-3 bg-white border border-gray-200 rounded-md">
                <div className="flex items-center gap-3">
                  <span className="text-lg">&#x1F511;</span>
                  <div>
                    <span className="font-medium">{githubPatCredential.label}</span>
                    <span className="text-sm text-gray-500 ml-2">github</span>
                  </div>
                  {githubPatCredential.expires_at && (
                    <span className="text-xs px-2 py-0.5 rounded bg-gray-100 text-gray-600">
                      expires {new Date(githubPatCredential.expires_at).toLocaleDateString()}
                    </span>
                  )}
                </div>
                <button
                  onClick={() => handleDelete(githubPatCredential)}
                  disabled={deletingId === githubPatCredential.id}
                  className="text-sm text-red-600 hover:text-red-800 disabled:opacity-50"
                >
                  {deletingId === githubPatCredential.id ? 'Removing...' : 'Remove'}
                </button>
              </div>
            ) : showPatForm ? (
              <RegisterGitHubPat
                onSuccess={handlePatSuccess}
                onCancel={() => setShowPatForm(false)}
              />
            ) : (
              <>
                <p className="text-sm text-gray-500 mb-3">
                  Register a fine-grained GitHub PAT so agents can create PRs and post comments as you.
                </p>
                <button
                  onClick={() => setShowPatForm(true)}
                  className="inline-flex items-center px-4 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-700"
                >
                  + Register GitHub PAT
                </button>
              </>
            )}
          </section>

          {/* Other Credentials Section */}
          {otherCredentials.length > 0 && (
            <section>
              <h2 className="text-lg font-semibold mb-3">Other Credentials</h2>
              <div className="space-y-2">
                {otherCredentials.map((cred) => (
                  <div
                    key={cred.id}
                    className="flex items-center justify-between p-3 bg-white border border-gray-200 rounded-md"
                  >
                    <div className="flex items-center gap-3">
                      <span className="text-lg">&#x1F511;</span>
                      <div>
                        <span className="font-medium">{cred.label}</span>
                        <span className="text-sm text-gray-500 ml-2">{cred.service}</span>
                      </div>
                    </div>
                    <button
                      onClick={() => handleDelete(cred)}
                      disabled={deletingId === cred.id}
                      className="text-sm text-red-600 hover:text-red-800 disabled:opacity-50"
                    >
                      {deletingId === cred.id ? 'Removing...' : 'Remove'}
                    </button>
                  </div>
                ))}
              </div>
            </section>
          )}

          {credentials.length === 0 && !showPatForm && (
            <p className="text-gray-500">
              No credentials registered yet. Connect an AWS account to get started.
            </p>
          )}
        </>
      )}
    </div>
  );
}
