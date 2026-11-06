import { useEffect, useState } from 'react';
import { automateSlackApp, getOAuthApp, registerOAuthApp, type OAuthAppStatus } from '@/services/oauthConnections';
import { extractCredentialError } from '@/services/credentials';

export function OAuthAppSetup({ provider, name, onRegistered }: {
  provider: string; name: string; onRegistered: () => void;
}) {
  const [app, setApp] = useState<OAuthAppStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [configurationToken, setConfigurationToken] = useState('');

  const refresh = async () => {
    const current = await getOAuthApp(provider);
    setApp(current);
    onRegistered();
  };

  const automate = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      setApp(await automateSlackApp(configurationToken));
      onRegistered();
    } catch (err: unknown) {
      setError(extractCredentialError(err));
      try { await refresh(); } catch { setApp(null); }
    } finally {
      setConfigurationToken('');
      setBusy(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    getOAuthApp(provider).then((result) => { if (!cancelled) setApp(result); })
      .catch((err: unknown) => { if (!cancelled) setError(extractCredentialError(err)); });
    return () => { cancelled = true; };
  }, [provider]);

  const register = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      setApp(await registerOAuthApp(provider, clientId, clientSecret));
      onRegistered();
    } catch (err: unknown) {
      setError(extractCredentialError(err));
      // A timed-out registration may have succeeded. Read status before another write.
      try { setApp(await getOAuthApp(provider)); onRegistered(); } catch { setApp(null); }
    } finally {
      setClientSecret('');
      setBusy(false);
    }
  };

  return <div className="mt-3 space-y-3 text-sm">
    {error && <p role="alert">{error}</p>}
    {!app && !error && <p role="status">Loading app setup…</p>}
    {app && <>
      <p>A platform administrator registers one app for this ADP deployment. Each user then connects their own account.</p>
      <a href={app.developer_url} target="_blank" rel="noopener noreferrer" className="text-blue-600 underline">Open {name} app settings</a>
      {provider === 'slack' && (app.status === 'setup_required' || app.automation_state === 'credentials_saved') && app.automation_state !== 'creating' && <form onSubmit={automate} className="space-y-3 rounded border p-3">
        <p>ADP can create the Slack app, register it, and save its callback automatically.</p>
        <p>In Slack app settings, open <strong>Your App Configuration Tokens</strong> and generate a token for your workspace. Paste the access token here. ADP uses it only for this setup and does not save it.</p>
        <label className="block">App configuration access token<input type="password" required autoComplete="new-password" maxLength={4096}
          value={configurationToken} onChange={(event) => setConfigurationToken(event.target.value)} className="mt-1 block w-full rounded border p-2 dark:bg-gray-800" /></label>
        <button type="submit" disabled={busy || !configurationToken} className="rounded border px-3 py-1 disabled:opacity-50">
          {busy ? 'Setting up…' : app.automation_state === 'credentials_saved' ? 'Resume Slack setup' : 'Create and register Slack app'}
        </button>
        <p className="text-gray-600 dark:text-gray-400">Your workspace may require an administrator to approve the app. Each user still approves their own account access.</p>
      </form>}
      {app.automation_state === 'creating' && <p role="alert">App creation has an uncertain result. Check your Slack apps and ask an operator to reconcile it before creating another app.</p>}
      {app.status === 'setup_required' && !app.automation_state && <details open={provider !== 'slack'}><summary className="cursor-pointer">{provider === 'slack' ? 'Use an existing app or set up manually' : 'Register provider app'}</summary><form onSubmit={register} className="mt-3 space-y-3">
        {provider === 'atlassian' ? <>
          <ol className="list-decimal space-y-2 pl-5">
            <li>Choose <strong>OAuth 2.0 (3LO)</strong> when creating the app in Atlassian.</li>
            <li>In <strong>Permissions</strong>, add the Jira API with <code>read:jira-work</code> and the User Identity API with <code>read:me</code>.</li>
            <li>Enable sharing under <strong>Distribution</strong> so other ADP users can connect.</li>
            <li>Copy the client ID and secret from the app’s <strong>Settings</strong> into this form and register the app.</li>
            <li>Save the callback URL ADP returns under the app’s <strong>Authorization</strong> settings.</li>
          </ol>
          <p>ADP requests <code>offline_access</code> during user consent to renew access between sessions; it is not an API permission to add in the console.</p>
        </> : <p>Create an OAuth app in the provider’s settings, then enter its client credentials here.
          {provider === 'slack' && ' Add the requested permissions as user token scopes.'}
          {' '}Allow these permissions: {app.scopes.join(', ')}. After registration, ADP supplies the callback URL to save in that app.</p>}
        {provider === 'slack' && app.manifest && <a target="_blank" rel="noopener noreferrer" className="block text-blue-600 underline"
          href={`https://api.slack.com/apps?new_app=1&manifest_json=${encodeURIComponent(JSON.stringify(app.manifest))}`}>Create Slack app with settings prefilled</a>}
        <label className="block">Client ID<input required maxLength={1024} value={clientId}
          onChange={(event) => setClientId(event.target.value)} className="mt-1 block w-full rounded border p-2 dark:bg-gray-800" /></label>
        <label className="block">Client secret<input required type="password" autoComplete="new-password" maxLength={4096} value={clientSecret}
          onChange={(event) => setClientSecret(event.target.value)} className="mt-1 block w-full rounded border p-2 dark:bg-gray-800" /></label>
        <button type="submit" disabled={busy || !clientId || !clientSecret} className="rounded border px-3 py-1 disabled:opacity-50">
          {busy ? 'Registering…' : `Register ${name} app`}
        </button>
      </form></details>}
      {app.callback_url && <>
        <p>{app.automation_state === 'ready' ? 'ADP saved this callback in Slack. You can now connect your account.' : 'Save this exact callback URL in the app’s authorization settings, then select Connect above to verify your account.'}</p>
        <label className="block">Callback URL<input readOnly value={app.callback_url} onFocus={(event) => event.target.select()}
          className="mt-1 block w-full rounded border p-2 dark:bg-gray-800" /></label>
      </>}
      {app.status === 'deployment_incomplete' && <p>The app exists, but deployment setup needs an operator update before accounts can connect.</p>}
      {app.status === 'unavailable' && <p>App setup is currently unavailable. Refresh to check again.</p>}
    </>}
    <button type="button" disabled={busy} onClick={() => { setError(''); refresh().catch((err: unknown) => setError(extractCredentialError(err))); }} className="underline">Refresh app setup</button>
  </div>;
}
