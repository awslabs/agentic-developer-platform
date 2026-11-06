/**
 * Credentials list page — manage vault credentials (AWS roles, API keys, etc.).
 *
 * Issue #562: Self-serve AWS account connect UI.
 * Issue #4746 (#4692 · R5): the Bedrock account selector is mounted in the AWS Accounts
 *   section — no new screen (ruling 2). Somebody who has just connected an account is
 *   already asking "may I send my model calls through it?", and this is where they ask it.
 *   The section's own rows now render `AwsConnectionRow`, the component the selector uses,
 *   so the two lists of AWS accounts on this page cannot describe an account differently
 *   (§6.6).
 *
 * URL: /settings/credentials
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { listCredentials, deleteCredential, extractCredentialError, getToolsConsent, setCredentialAgentConsent, setToolsConsent, setToolsConsentDefault, type CredentialItem, type ToolsConsent } from '@/services/credentials';
import { AwsConnectionRow } from '@/components/aws/AwsConnectionRow';
import { BedrockAccountSelector } from '@/components/aws/BedrockAccountSelector';

export default function SettingsCredentials() {
  const [credentials, setCredentials] = useState<CredentialItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<'credentials' | 'tools'>('credentials');
  const [tools, setTools] = useState<ToolsConsent | null>(null);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [savingConsent, setSavingConsent] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

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

  useEffect(() => {
    if (activeTab !== 'tools') return;
    let cancelled = false;
    setToolsLoading(true);
    setError(null);
    getToolsConsent().then((result) => {
      if (!cancelled) setTools(result);
    }).catch((err: unknown) => {
      if (!cancelled) {
        setTools(null);
        if ((err as { status?: number }).status !== 404) setError(extractCredentialError(err));
      }
    }).finally(() => {
      if (!cancelled) setToolsLoading(false);
    });
    return () => { cancelled = true; };
  }, [activeTab]);

  const handleCredentialConsent = async (cred: CredentialItem) => {
    setSavingConsent(cred.id);
    setError(null);
    try {
      const updated = await setCredentialAgentConsent(cred.id, !cred.agent_consent);
      setCredentials((previous) => previous.map((item) => item.id === cred.id ? updated : item));
    } catch (err: unknown) {
      setError(extractCredentialError(err));
    } finally {
      setSavingConsent(null);
    }
  };

  const defaultMode = tools?.consent_mode === 'default';
  const canUseDefaults = tools?.tenant_consent_default === 'granted';

  const saveTools = async (key: string, write: () => Promise<ToolsConsent>) => {
    setSavingConsent(key);
    setError(null);
    try {
      setTools(await write());
    } catch (err: unknown) {
      if ((err as { status?: number }).status === 409) {
        try { setTools(await getToolsConsent()); } catch { setTools(null); }
      }
      setError(extractCredentialError(err));
    } finally {
      setSavingConsent(null);
    }
  };

  const handleToolConsent = async (tool: string) => {
    if (!tools) return;
    if (defaultMode) {
      // #8387: in the organization-default mode a switch records an opt-out, not a consent.
      const optedOut = tools.opted_out_tools ?? [];
      const next = tools.consented_tools.includes(tool) ? [...optedOut, tool] : optedOut.filter((name) => name !== tool);
      await saveTools(tool, () => setToolsConsentDefault(next, tools.revision));
      return;
    }
    const next = tools.consented_tools.includes(tool)
      ? tools.consented_tools.filter((name) => name !== tool)
      : [...tools.consented_tools, tool];
    await saveTools(tool, () => setToolsConsent(next, tools.revision));
  };

  const handleConsentMode = async () => {
    if (!tools) return;
    if (defaultMode) {
      // Keep exactly what is on today, but from now on the owner manages the list.
      await saveTools('consent-mode', () => setToolsConsent(tools.consented_tools, tools.revision));
    } else {
      await saveTools('consent-mode', () => setToolsConsentDefault([], tools.revision));
    }
  };

  const credentialToggle = (cred: CredentialItem) => cred.scope === 'user' ? (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={cred.agent_consent} disabled={savingConsent !== null}
        onChange={() => handleCredentialConsent(cred)} aria-label={`Available to agents: ${cred.label}`} />
      Available to agents
    </label>
  ) : null;

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

  // Separate credentials by type
  const awsCredentials = credentials.filter((c) => c.service === 'aws');
  const isManagedConnection = (c: CredentialItem) => c.scope === 'user' && ['github', 'jira', 'slack'].includes(c.service)
    && c.credential_type === 'oauth_token' && c.provider === 'agentcore-identity';
  const otherCredentials = credentials.filter(
    (c) => c.service !== 'aws' && !isManagedConnection(c),
  );

  return (
    <div className="max-w-4xl mx-auto p-6">
      <h1 className="text-2xl font-bold mb-6">My Credentials</h1>
      <p className="mb-6 text-sm text-gray-600 dark:text-gray-400">
        Manage GitHub, Jira and Slack in{' '}
        <Link to="/settings/connections" className="text-blue-600 underline">Connections</Link>.
      </p>

      <div role="tablist" aria-label="Agent access" className="flex gap-4 mb-6 border-b">
        <button type="button" role="tab" aria-selected={activeTab === 'credentials'}
          onClick={() => { setError(null); setActiveTab('credentials'); }}>Credentials</button>
        <button type="button" role="tab" aria-selected={activeTab === 'tools'}
          onClick={() => setActiveTab('tools')}>Tools</button>
      </div>

      {error && (
        <div className="mb-4 bg-red-50 border border-red-200 rounded-md p-4">
          <p className="text-sm text-red-800">{error}</p>
        </div>
      )}

      {activeTab === 'tools' ? (
        <section role="tabpanel" aria-label="Tools">
          <h2 className="text-lg font-semibold mb-3">Agent tools</h2>
          {defaultMode ? (
            <p className="text-sm text-gray-600 mb-4">Tools your organization enables for your team are on by default. Switch off any you do not want agents to use.</p>
          ) : (
            <p className="text-sm text-gray-600 mb-4">Only tools you allow here and your organization permits are available to agents.</p>
          )}
          {toolsLoading ? <p>Loading tools...</p> : tools?.allowed_tools.length ? (
            <div className="space-y-3">
              {tools.allowed_tools.map((tool) => (
                <label key={tool} className="flex items-center gap-2">
                  <input type="checkbox" checked={tools.consented_tools.includes(tool)}
                    disabled={savingConsent !== null} onChange={() => handleToolConsent(tool)}
                    aria-label={`Available to agents: ${tool}`} />
                  {tool}
                  {defaultMode && tools.entitled_tools && !tools.entitled_tools.includes(tool) && (
                    <span className="text-xs text-gray-500">(not enabled by your organization)</span>
                  )}
                </label>
              ))}
              {(defaultMode || canUseDefaults) && (
                <button type="button" className="mt-2 text-sm underline" disabled={savingConsent !== null} onClick={() => void handleConsentMode()}>
                  {defaultMode ? 'Manage consent explicitly' : 'Use organization defaults'}
                </button>
              )}
            </div>
          ) : !error && <p>No tools currently available.</p>}
        </section>
      ) : isLoading ? (
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
                  <AwsConnectionRow
                    key={cred.id}
                    label={cred.label}
                    accountId={cred.scopes?.account_id}
                    status={cred.scopes?.status}
                  >
                    {credentialToggle(cred)}
                    <button
                      onClick={() => handleDelete(cred)}
                      disabled={deletingId === cred.id}
                      className="text-sm text-red-600 hover:text-red-800 disabled:opacity-50"
                    >
                      {deletingId === cred.id ? 'Removing...' : 'Remove'}
                    </button>
                  </AwsConnectionRow>
                ))}
              </div>
            )}
            <Link
              to="/settings/credentials/aws/connect"
              className="inline-flex items-center px-4 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-700"
            >
              + Connect AWS Account
            </Link>

            {/* #4746 (§6.4). Inside this section rather than a section of its own: the
                question it answers is about these accounts, and ruling 2 forbids a new
                screen. Rendered even with no accounts connected — "your calls go to the
                platform's account" is the answer then, and it is the fact that makes
                connecting one worth doing. */}
            <BedrockAccountSelector />
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
                      {cred.expires_at && (
                        <span className="text-xs px-2 py-0.5 rounded bg-gray-100 text-gray-600">
                          expires {new Date(cred.expires_at).toLocaleDateString()}
                        </span>
                      )}
                    </div>
                    {credentialToggle(cred)}
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
