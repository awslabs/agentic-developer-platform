/**
 * Credentials list page — manage vault credentials (AWS roles, API keys, etc.).
 *
 * Issue #562: Self-serve AWS account connect UI.
 *
 * URL: /settings/credentials
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { listCredentials, deleteCredential, type CredentialItem } from '@/services/credentials';

export default function SettingsCredentials() {
  const [credentials, setCredentials] = useState<CredentialItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const loadCredentials = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const creds = await listCredentials();
      setCredentials(creds);
    } catch (err: unknown) {
      const message = (err as { message?: string })?.message || 'Failed to load credentials';
      setError(message);
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
        ? 'This removes ADP\'s record of this AWS role. The IAM role still exists in your AWS account \u2014 delete the CloudFormation stack there if you no longer need it.'
        : `Remove credential "${cred.label}"? This cannot be undone.`,
    );
    if (!confirmed) return;

    setDeletingId(cred.id);
    try {
      await deleteCredential(cred.id);
      setCredentials((prev) => prev.filter((c) => c.id !== cred.id));
    } catch (err: unknown) {
      const message = (err as { message?: string })?.message || 'Failed to delete credential';
      setError(message);
    } finally {
      setDeletingId(null);
    }
  };

  // Separate AWS credentials from others
  const awsCredentials = credentials.filter((c) => c.service === 'aws');
  const otherCredentials = credentials.filter((c) => c.service !== 'aws');

  return (
    <div className="max-w-4xl mx-auto p-6">
      <h1 className="text-2xl font-bold mb-6">My Credentials</h1>

      {error && (
        <div className="mb-4 bg-red-50 border border-red-200 rounded-md p-4">
          <p className="text-sm text-red-800">{error}</p>
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

          {credentials.length === 0 && (
            <p className="text-gray-500">
              No credentials registered yet. Connect an AWS account to get started.
            </p>
          )}
        </>
      )}
    </div>
  );
}
