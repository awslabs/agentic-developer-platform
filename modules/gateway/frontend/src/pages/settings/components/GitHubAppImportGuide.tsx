import { useId, useState } from 'react';
import { getGitHubAppSetupGuide, type GitHubAppSetupGuide } from '@/services/connections';

const labels: Record<string, string> = {
  homepage_url: 'Homepage URL', callback_url: 'Callback URL',
  setup_url: 'Setup URL', webhook_url: 'Webhook URL',
  contents: 'Repository → Contents', issues: 'Repository → Issues',
  pull_requests: 'Repository → Pull requests', checks: 'Repository → Checks',
  metadata: 'Repository → Metadata', members: 'Organization → Members',
  issue_comment: 'Issue comment', pull_request: 'Pull request',
  pull_request_review: 'Pull request review',
  pull_request_review_comment: 'Pull request review comment', label: 'Label',
};

const permissionDescriptions: Record<string, string> = {
  checks: 'Publish check results on commits and pull requests.',
  contents: 'Read repository code and create changes.',
  issues: 'Read and respond to issues and comments.',
  metadata: 'Discover repository details.',
  pull_requests: 'Create and update pull requests.',
  members: 'Check organization membership for access decisions.',
};
const eventDescriptions: Record<string, string> = {
  issues: 'Respond when issues are opened or updated.',
  issue_comment: 'Receive requests and follow-up instructions in comments.',
  label: 'Track label changes used in repository workflows.',
  pull_request: 'Respond to pull request lifecycle changes.',
  pull_request_review: 'Receive submitted reviews and review decisions.',
  pull_request_review_comment: 'Receive feedback on specific lines of code.',
};
const permissionOrder = ['checks', 'contents', 'issues', 'metadata', 'pull_requests', 'members'];
const eventOrder = ['issue_comment', 'issues', 'label', 'pull_request', 'pull_request_review', 'pull_request_review_comment'];
function formOrder(keys: string[], order: string[]) {
  return [...keys].sort((a, b) => {
    const rank = (key: string) => order.includes(key) ? order.indexOf(key) : order.length;
    return rank(a) - rank(b) || a.localeCompare(b);
  });
}

export function GitHubAppImportGuide() {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [guide, setGuide] = useState<GitHubAppSetupGuide | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [copyStatus, setCopyStatus] = useState('');
  async function load() {
    setLoading(true);
    setError(false);
    try { setGuide(await getGitHubAppSetupGuide()); }
    catch { setError(true); }
    finally { setLoading(false); }
  }
  async function copy(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyStatus(`${label} copied.`);
    } catch { setCopyStatus('Could not copy. Select and copy the URL below.'); }
  }
  function urlField(key: 'homepage_url' | 'callback_url' | 'setup_url' | 'webhook_url', description: string) {
    const value = guide?.[key];
    return <>
      <label htmlFor={`${id}-${key}`} className="block font-medium">{labels[key]}</label>
      <p id={`${id}-${key}-description`} className="mb-2">{description}</p>
      {value ? <div className="flex gap-2">
        <input id={`${id}-${key}`} aria-describedby={`${id}-${key}-description`} readOnly value={value}
          onFocus={event => event.target.select()}
          className="min-w-0 flex-1 rounded border border-gray-300 bg-transparent p-2 font-mono text-xs dark:border-gray-600" />
        <button type="button" className="underline" aria-label={`Copy ${labels[key]}`} onClick={() => void copy(labels[key], value)}>Copy</button>
      </div> : <p role="status">Not available. Ask your deployment administrator to complete this setting before importing.</p>}
    </>;
  }
  return <div className="mt-3 text-sm">
    <button type="button" aria-expanded={open} aria-controls={id}
      className="text-primary-600 underline dark:text-primary-400"
      onClick={() => { setOpen(!open); if (!open && !guide && !loading) void load(); }}>
      How to configure your GitHub App
    </button>
    {open && <div id={id} className="mt-3 space-y-4 rounded-lg border border-gray-200 p-4 text-gray-700 dark:border-gray-700 dark:text-gray-300">
      <h3 className="font-semibold">Configure an App before importing</h3>
      <p>In your GitHub organization, open Settings → Developer settings → GitHub Apps → New GitHub App.
        Choose a unique name for this environment. For an existing App, open its settings.</p>
      <p>Use an App dedicated to this deployment. Changing a shared App’s webhook URL can interrupt another environment.</p>
      {loading && <p role="status">Loading this deployment’s settings…</p>}
      {error && <p role="alert">Could not load the setup settings. <button type="button" className="underline" onClick={() => void load()}>Retry</button></p>}
      {guide && <>
        <p>Follow these fields in the order shown on GitHub’s “New GitHub App” form.</p>
        <ol className="list-outside list-decimal space-y-4 pl-5" aria-label="GitHub App setup fields">
          <li><strong>GitHub App name</strong><p>Choose a globally unique name for this environment, such as <code>your-org-adp-pre-prod</code>. This identifies the App when it acts on GitHub.</p></li>
          <li><strong>Description</strong><p>Optional. For example: “ADP integration for our pre-production environment.” Users see this when installing the App.</p></li>
          <li>{urlField('homepage_url', 'Points users to this ADP deployment from the App’s GitHub page.')}</li>
          <li>{urlField('callback_url', 'Returns GitHub user authorization to ADP to complete “Sign in with GitHub”. This is the OAuth redirect URI; use this exact URL. It is different from the post-install Setup URL below.')}</li>
          <li><strong>Expire user authorization tokens</strong><p>Leave checked. Limits the lifetime of GitHub user access tokens.</p></li>
          <li><strong>Request user authorization (OAuth) during installation</strong><p>Leave unchecked. ADP handles sign-in separately, and GitHub keeps the Setup URL available for connecting the installation.</p></li>
          <li><strong>Enable Device Flow</strong><p>Leave unchecked. GitHub sign-in uses the browser callback above.</p></li>
          <li>{urlField('setup_url', 'Returns users to ADP after installation so ADP can connect the GitHub installation to their workspace.')}</li>
          <li><strong>Redirect on update</strong><p>Check. Returns users to ADP after they change the repositories available to an installation.</p></li>
          <li><strong>Webhook → Active</strong><p>Check. Enables delivery of GitHub events to ADP workers.</p></li>
          <li>{urlField('webhook_url', 'Sends repository events to this deployment’s webhook receiver so ADP can respond to issues, comments, and pull requests.')}</li>
          <li><strong>Webhook secret</strong><p>Generate and save a strong random secret. Enter the same value here and in ADP’s import form so ADP can verify that webhook deliveries came from GitHub.</p></li>
          <li><strong>SSL verification</strong><p>Select “Enable SSL verification”. GitHub verifies the webhook receiver’s HTTPS certificate.</p></li>
          <li><strong>Permissions</strong><p>Expand Repository permissions, then Organization permissions. Set the following; leave other permissions at their defaults.</p>
            <table className="mt-2 w-full text-left"><thead><tr><th scope="col">Permission</th><th scope="col">Access</th><th scope="col">Purpose</th></tr></thead>
              <tbody>{formOrder(Object.keys(guide.permissions), permissionOrder).map(key => <tr key={key}>
                <td className="py-1 pr-2">{labels[key] ?? key}</td>
                <td className="pr-2">{guide.permissions[key] === 'write' ? 'Read and write' : 'Read-only'}</td>
                <td>{permissionDescriptions[key] ?? 'Required by this deployment’s GitHub integration.'}</td>
              </tr>)}</tbody>
            </table>
          </li>
          <li><strong>Subscribe to events</strong><p>Select these events after setting permissions; GitHub uses the permissions to determine which events are available.</p>
            <ul className="mt-2 list-inside list-disc space-y-1">{formOrder(guide.events, eventOrder).map(event => <li key={event}>
              <span className="font-medium">{event === 'issues' ? 'Issues' : (labels[event] ?? event)}</span> — {eventDescriptions[event] ?? 'Notify ADP about this event.'}
            </li>)}</ul>
          </li>
          <li><strong>Where can this GitHub App be installed?</strong><p>Choose “Only on this account” for your organization. Choose “Any account” if other organizations need to install this App. This controls who can install it.</p></li>
          <li><strong>Create GitHub App</strong><p>Click to save the registration. Creating the App does not yet connect it to ADP.</p></li>
        </ol>
        <p role="status">{copyStatus}</p>
        <h4 className="font-semibold">After creating the App</h4>
        <ol className="list-outside list-decimal space-y-2 pl-5">
          <li>Copy the App ID and Client ID from its settings. Generate a client secret for GitHub sign-in and a private key (PEM) so ADP can authenticate as the App.</li>
          <li>In “Connect an existing App”, enter the App ID, private key, and saved webhook secret. Include the Client ID and client secret to enable GitHub sign-in.</li>
          <li>After importing, use ADP’s install action to install the App on your chosen repositories and return to ADP.</li>
        </ol>
        <a href="https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app" target="_blank" rel="noopener noreferrer" className="inline-block underline">GitHub’s App registration documentation ↗</a>
      </>}
    </div>}
  </div>;
}
