import { useEffect, useRef, useState } from 'react';
import { Modal } from '@/components/ui';
import { ProviderIcon } from '@/components/ui/ProviderIcon';
import Connections from '@/pages/settings/Connections';
import ConnectAws from '@/pages/settings/ConnectAws';
import { useRevalidatingFeaturesQuery } from '@/hooks/useFeatures';
import JiraContextSources from './JiraContextSources';
import { registerProfileCredential } from '@/services/credentials';
import type { ConnectionService, ProductConnectionContext } from '@/services/productConnectionSetup';

const services: { id: ConnectionService; name: string; description: string }[] = [
  { id: 'github', name: 'GitHub', description: 'Connect repositories through the GitHub App.' },
  { id: 'aws', name: 'AWS account', description: 'Create an access role and verify the connection.' },
  { id: 'jira', name: 'Jira', description: 'Set up shared access, then choose projects and boards.' },
  { id: 'slack', name: 'Slack', description: 'Connect your Slack account for agent tools.' },
  { id: 'sonarqube', name: 'SonarQube', description: 'Register read-only project quality access for Profile.' },
  { id: 'jenkins', name: 'Jenkins', description: 'Register read-only CI job access for Profile.' },
  { id: 'argocd', name: 'Argo CD', description: 'Register read-only application access for Profile.' },
  { id: 'gitlab', name: 'GitLab', description: 'Register an owned repository connection for Profile.' },
  { id: 'pagerduty', name: 'PagerDuty', description: 'Register read-only service access for Profile.' },
  { id: 'linear', name: 'Linear', description: 'Register read-only team access for Profile.' },
  { id: 'slack_profile', name: 'Slack channels', description: 'Register a separately consented, read-only test-channel token for Profile.' },
];

type Props = ProductConnectionContext & { onClose: () => void; onSaved: () => void; initialService?: ConnectionService };

export default function ProductConnectionSetup({ productId, projectId, onClose, onSaved, initialService }: Props) {
  const { data: features } = useRevalidatingFeaturesQuery();
  const [service, setService] = useState<ConnectionService | null>(initialService ?? null);
  const available = services.filter(item => ['github', 'aws', 'jira', 'slack'].includes(item.id) || features?.devlake_connectors === true);
  const selected = available.find(item => item.id === service);
  const content = useRef<HTMLDivElement>(null);
  useEffect(() => { content.current?.focus(); }, [service]);

  return <Modal isOpen onClose={onClose} title={selected ? `Connect ${selected.name}` : 'Add connection'} size={selected ? 'xl' : 'md'} closeOnOverlayClick={false}>
    <div ref={content} tabIndex={-1} className="product-connection-setup space-y-4 outline-none">
      {!selected ? <>
        <SetupProgress step={1} />
        <p>Choose a service to set it up here. Existing connections remain available in your Product.</p>
        <div className="product-add-connection-options">
          {available.map(item => <button key={item.id} type="button" aria-label={item.name} onClick={() => setService(item.id)}>
            <ProviderIcon provider={item.id} /><span><strong className="block">{item.name}</strong><span className="block text-sm">{item.description}</span></span>
          </button>)}
        </div>
      </> : <ConnectionStep key={selected.id} service={selected.id} productId={productId} projectId={projectId} onSaved={onSaved} onClose={onClose} onBack={() => setService(null)} />}
    </div>
  </Modal>;
}

function SetupProgress({ step }: { step: number }) {
  return <ol aria-label="Connection setup progress" className="flex flex-wrap gap-4 text-sm">
    {['Choose service', 'Configure and verify', 'Ready'].map((label, index) => <li key={label} aria-current={step === index + 1 ? 'step' : undefined}>{index + 1}. {label}</li>)}
  </ol>;
}

function ConnectionStep({ service, productId, projectId, onSaved, onClose, onBack }: Props & { service: ConnectionService; onBack: () => void }) {
  const [ready, setReady] = useState(false);
  const connected = () => { setReady(true); onSaved(); };
  const connector = !['github', 'aws', 'jira', 'slack'].includes(service);
  return <>
        <SetupProgress step={ready ? 3 : 2} />
        <p>{connector ? 'Register a read-only credential in the existing ADP vault, then choose and verify its native scope in Profile. This does not grant Insights collection.'
          : service === 'jira' ? 'Shared access belongs to this Product. A platform administrator manages the credentials; members choose the Jira sources.'
          : service === 'slack' ? 'This connects your Slack account with the permissions you approve. Product members do not inherit your account access.'
            : service === 'aws' ? 'Create and verify access here. AWS opens in another tab when you authorize the role.'
              : 'Use the existing GitHub App to connect repositories. GitHub may ask you to authorize access; setup returns here afterward.'}</p>
        {service === 'jira' && <JiraContextSources productId={productId} projectId={projectId} onSaved={onSaved} setup onReadyChange={setReady} />}
        {service === 'aws' && <ConnectAws embedded onConnected={connected} />}
        {connector && <ConnectorCredentialSetup service={service} onConnected={connected} />}
        {(service === 'github' || service === 'slack') && <Connections service={service} productContext={{ productId, projectId }} onReadyChange={setReady} />}
        {ready && <p role="status" className="rounded border border-green-300 bg-green-50 p-3 text-green-900">{connector ? 'Credential registered. Choose and verify a native source before using its Profile reference.' : service === 'jira' ? 'Jira sources are connected for this Product.' : service === 'slack' ? 'Your Slack account is verified and available to agent tools.' : 'Connection available. Return to your Product to choose its sources.'}</p>}
        <div className="flex flex-wrap justify-between gap-3 border-t pt-4">
          <button type="button" className="rounded border px-3 py-2" onClick={onBack}>Choose another service</button>
          <button type="button" className="rounded border px-3 py-2" onClick={onClose}>{ready ? 'Done — return to Product' : 'Close setup'}</button>
        </div>
      </>;
}

function ConnectorCredentialSetup({ service, onConnected }: { service: ConnectionService; onConnected: () => void }) {
  const [label, setLabel] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [username, setUsername] = useState('');
  const [token, setToken] = useState('');
  const [linearCredentialType, setLinearCredentialType] = useState<'api_key' | 'bearer'>('api_key');
  const [operationId] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const provider = service === 'slack_profile' ? 'slack' : service;
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setError('');
    try {
      const url = new URL(endpoint);
      if (url.protocol !== 'https:' || !url.hostname || url.username || url.password || url.search || url.hash || !label.trim() || !token) throw new Error('invalid');
      setBusy(true);
      await registerProfileCredential(operationId, {
        service: provider, label: label.trim(),
        credential_type: service === 'jenkins' ? 'basic_auth' : service === 'slack_profile' ? 'oauth_token' : service === 'linear' ? linearCredentialType : 'bearer',
        scope_hint: 'user', value: JSON.stringify({ url: endpoint, token, ...(service === 'jenkins' ? { username } : {}) }),
        ...(service === 'slack_profile' ? { scopes: { oauth_scopes: ['channels:read', 'groups:read'] } } : {}),
      });
      setToken('');
      onConnected();
    } catch {
      setError('Registration failed or inputs changed. Check the credential list before retrying this operation with identical values.');
    } finally {
      setBusy(false);
    }
  };
  return <form onSubmit={(event) => void submit(event)} className="space-y-3">
    <label className="block">Connection label<input required value={label} disabled={busy} onChange={event => setLabel(event.target.value)} /></label>
    <label className="block">Provider HTTPS endpoint<input required type="url" value={endpoint} disabled={busy} onChange={event => setEndpoint(event.target.value)} /></label>
    {service === 'jenkins' && <label className="block">Username<input required value={username} disabled={busy} onChange={event => setUsername(event.target.value)} /></label>}
    {service === 'linear' && <label className="block">Linear credential type<select value={linearCredentialType} disabled={busy} onChange={event => setLinearCredentialType(event.target.value === 'api_key' ? 'api_key' : 'bearer')}>
      <option value="api_key">Personal API key</option>
      <option value="bearer">OAuth Bearer token</option>
    </select></label>}
    <label className="block">Read-only token<input required type="password" autoComplete="off" value={token} disabled={busy} onChange={event => setToken(event.target.value)} /></label>
    {service === 'slack_profile' && <p>Use only a token consented for channels:read and groups:read on the selected test channel. Missing permissions fail discovery; this does not enable posting or DMs.</p>}
    {error && <p role="alert">{error}</p>}
    <button type="submit" disabled={busy}>Register credential</button>
  </form>;
}
