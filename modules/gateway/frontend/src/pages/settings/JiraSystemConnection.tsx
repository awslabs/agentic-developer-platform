import { useEffect, useRef, useState } from 'react';
import { listProducts, type ProductSummary } from '@/services/products';
import { disconnectJiraSystemConnection, verifyJiraSystemConnection, listJiraSystemConnections, saveJiraSystemConnection, type JiraSystemConnection as Connection } from '@/services/jiraContext';
import { startJiraForgePairing, updateJiraConnectionAccess, type ForgePairing } from '@/services/jiraContext';

type Props = { productId?: string; onChanged?: () => void; initialSetup?: boolean };

export default function JiraSystemConnection({ productId, onChanged, initialSetup = false }: Props) {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [products, setProducts] = useState<ProductSummary[]>([]);
  const [selected, setSelected] = useState<Connection | null>(null);
  const [editing, setEditing] = useState(initialSetup);
  const [label, setLabel] = useState('Shared Jira');
  const [site, setSite] = useState('');
  const [key, setKey] = useState('');
  const [expiry, setExpiry] = useState('');
  const [allowed, setAllowed] = useState<string[]>(productId ? [productId] : []);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [ready, setReady] = useState(false);
  const [forgeAvailable, setForgeAvailable] = useState(false);
  const [method, setMethod] = useState<'service_account' | 'forge_app'>('service_account');
  const [pairing, setPairing] = useState<ForgePairing | null>(null);
  const setupEdited = useRef(false);

  async function reload() {
    const page = await listJiraSystemConnections();
    setConnections(page.connections);
    setForgeAvailable(page.forge_available === true);
    setReady(true);
    onChanged?.();
  }
  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const page = await listJiraSystemConnections();
        const choices: ProductSummary[] = [];
        let cursor: string | undefined;
        while (!productId && active) {
          const products = await listProducts(cursor);
          choices.push(...products.items);
          cursor = products.next_cursor ?? undefined;
          if (!cursor) break;
        }
        if (active) {
          setConnections(page.connections); setForgeAvailable(page.forge_available === true); setProducts(choices); setReady(true);
          if (initialSetup && page.forge_available === true && !setupEdited.current) setMethod('forge_app');
        }
      } catch { if (active) setMessage('Shared Jira setup is unavailable in this deployment.'); }
    })();
    return () => { active = false; };
  }, [productId, initialSetup]);

  useEffect(() => {
    if (!pairing) return;
    let active = true;
    let running = false;
    const timer = window.setInterval(async () => {
      if (running) return;
      if (new Date(pairing.expires_at) <= new Date()) {
        setPairing(null); setMessage('The connection code expired. Reconnect the pending app connection to generate a new code.');
        return;
      }
      running = true;
      try {
        const page = await listJiraSystemConnections();
        if (!active) return;
        setConnections(page.connections);
        const connection = page.connections.find(item => item.id === pairing.connection.id);
        if (connection?.status === 'ready') {
          setPairing(null); setMessage(productId ? 'ADP app connected. Choose a Jira project below.' : 'ADP app connected. Choose its Jira projects in Product connections.');
          onChanged?.();
        } else if (!connection || connection.status === 'revoked') setPairing(null);
      } catch { /* Keep the code until expiry; never claim a failed poll is a successful connection. */ }
      finally { running = false; }
    }, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [pairing, productId, onChanged]);

  function edit(connection: Connection | null) {
    setSelected(connection); setEditing(true); setKey(''); setMessage('');
    setLabel(connection?.label ?? 'Shared Jira'); setSite(connection?.site_id ?? '');
    setAllowed(connection?.product_ids ?? (productId ? [productId] : [])); setExpiry('');
    setMethod(connection?.connection_method ?? (forgeAvailable ? 'forge_app' : 'service_account'));
  }
  async function copyPairingCode() {
    if (!pairing) return;
    try {
      if (!navigator.clipboard) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(pairing.pairing_code);
      setMessage('Connection code copied. Paste it into the ADP app in Jira.');
    } catch { setMessage('Select the connection code and copy it manually.'); }
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true); setMessage('');
    const apiKey = key;
    setKey('');
    try {
      if (method === 'forge_app') {
        if (selected?.status === 'ready') {
          await updateJiraConnectionAccess(selected.id, { label, product_ids: allowed, expected_revision: selected.revision });
          setMessage('Product access updated.');
        } else {
          const result = await startJiraForgePairing({ label, product_ids: allowed,
            ...(selected ? { connection_id: selected.id, expected_revision: selected.revision } : {}),
          });
          setPairing(result);
          setMessage('Install or open the ADP app in Jira and enter the connection code below.');
        }
      } else {
      await saveJiraSystemConnection({ label, site_id: site.trim(), api_key: apiKey,
        expires_at: new Date(`${expiry}T00:00:00Z`).toISOString(), product_ids: allowed,
        ...(selected ? { expected_revision: selected.revision } : {}),
      }, selected?.id);
      setMessage(productId ? 'Shared Jira connection verified. Choose a Jira project below.' : 'Shared Jira connection verified. Choose its Jira projects in Product connections.');
      }
      setEditing(false); setSelected(null);
    } catch {
      setEditing(false); setSelected(null);
      setMessage(method === 'forge_app' ? 'Could not prepare the app connection. Refresh its status before retrying.' : 'Could not verify the connection. Check the service account token, site ID, expiry and Atlassian MCP permissions. Refresh before retrying.');
    } finally {
      try { await reload(); } catch { setReady(false); }
      setBusy(false);
    }
  }
  async function verify(connection: Connection) {
    setBusy(true); setMessage('');
    try { await verifyJiraSystemConnection(connection.id); await reload(); setMessage('Shared Jira identity and current tools verified.'); }
    catch { setMessage('Verification failed. Check the app or service account’s Jira access and connection status.'); }
    finally { setBusy(false); }
  }
  async function disconnect(connection: Connection) {
    setBusy(true); setMessage('');
    try {
      const result = await disconnectJiraSystemConnection(connection.id);
      await reload();
      setMessage(result.provider_cleanup_pending ? 'Access revoked. Credential cleanup is pending; use Retry cleanup.' : 'Shared Jira access revoked.');
    } catch { setMessage('Could not confirm revocation. Refresh status before retrying.'); }
    finally { setBusy(false); }
  }
  async function shareExisting(connection: Connection) {
    if (!productId) return;
    setBusy(true); setMessage('');
    try {
      await updateJiraConnectionAccess(connection.id, { label: connection.label, product_ids: [...connection.product_ids, productId], expected_revision: connection.revision });
      setEditing(false); setKey(''); await reload();
      setMessage('Shared Jira access enabled for this Product. Choose a Jira project below.');
    } catch { setMessage('Could not enable this connection. Refresh its status before retrying.'); }
    finally { setBusy(false); }
  }
  const visibleConnections = productId ? connections.filter(connection => connection.product_ids.includes(productId)) : connections;
  const reusableConnections = productId && initialSetup ? connections.filter(connection => !connection.product_ids.includes(productId) && connection.status === 'ready' && new Date(connection.expires_at) > new Date()) : [];
  const Wrapper = productId ? 'section' : 'details';
  const Heading = productId ? 'h4' : 'summary';
  return <Wrapper className="mt-4 border-t pt-3">
    <Heading className="font-medium">{productId ? 'Shared Jira access for this Product' : 'Shared Jira access for Products'}</Heading>
    {!initialSetup && <p className="mt-2 text-sm">Share Jira access with Product members and their agents through {forgeAvailable ? 'an installed app or a service account' : 'a service account'}.</p>}
    {message && <p aria-live="polite" className="mt-2 text-sm">{message}</p>}
    {!ready && !message && <p role="status">Checking shared Jira setup…</p>}
    {!ready && message && <button type="button" disabled={busy} className="underline" onClick={() => { setBusy(true); void reload().then(() => setMessage('')).catch(() => setMessage('Shared Jira setup is unavailable. Check your access and try again.')).finally(() => setBusy(false)); }}>Retry setup</button>}
    {visibleConnections.map(connection => <div className="mt-3 rounded border p-3" key={connection.id}>
      <p>{connection.label} · {new Date(connection.expires_at) <= new Date() && connection.status === 'ready' ? 'expired' : connection.status}</p>
      <p className="text-sm">{connection.connection_method === 'forge_app' ? 'ADP app · Automatic token refresh' : `Service account · Expires ${new Date(connection.expires_at).toLocaleDateString()}`} · {connection.product_ids.length} Products</p>
      {connection.status === 'ready' && <button type="button" disabled={busy} className="mr-3 underline" onClick={() => void verify(connection)}>Verify again</button>}
      {(!productId || connection.connection_method !== 'forge_app' || connection.status !== 'ready') && <button type="button" disabled={busy} className="mr-3 underline" onClick={() => edit(connection)}>{connection.connection_method === 'forge_app' ? (connection.status === 'ready' ? 'Change Product access' : 'Reconnect app') : productId ? 'Replace token' : 'Replace token or change access'}</button>}
      {!productId && <button type="button" disabled={busy} className="underline" onClick={() => void disconnect(connection)}>{connection.status === 'revoked' ? 'Retry cleanup' : 'Revoke shared access'}</button>}
    </div>)}
    {productId && !initialSetup && <p className="mt-2 text-sm">To manage sharing and revocation across Products, use <a href="/settings/connections" className="underline">Settings → Connections</a>.</p>}
    {ready && reusableConnections.length > 0 && <details className="mt-3 rounded border p-3">
      <summary>Use an existing shared Jira connection</summary>
      <p className="text-sm">Enable the same Jira access for members of this Product. Existing Product access is preserved.</p>
      {reusableConnections.map(connection => <div className="mt-2" key={connection.id}>
        <span>{connection.label} </span><button type="button" disabled={busy} className="underline" onClick={() => void shareExisting(connection)}>Use {connection.label} for this Product</button>
      </div>)}
    </details>}
    {pairing && <div className="mt-3 space-y-2 rounded border p-3">
      <a href={pairing.install_url} target="_blank" rel="noreferrer" className="underline">Install or open ADP app in Jira</a>
      <label className="block">Connection code<input readOnly autoComplete="off" value={pairing.pairing_code} onFocus={event => event.target.select()} className="ml-2 rounded border p-1 dark:bg-gray-900" /></label>
      <button type="button" className="underline" onClick={() => void copyPairingCode()}>Copy connection code</button>
      <p className="text-sm">Enter this code in the app’s Configure page. It expires at {new Date(pairing.expires_at).toLocaleTimeString()}. Waiting for Jira verification…</p>
      <button type="button" disabled={busy} className="underline" onClick={() => void disconnect(pairing.connection)}>Cancel setup</button>
    </div>}
    {ready && !editing && !pairing && <button type="button" disabled={busy} className="mt-3 rounded border px-3 py-1" onClick={() => edit(null)}>Set up shared Jira</button>}
    {editing && <form onSubmit={save} onChange={() => { setupEdited.current = true; }} className="mt-3 space-y-3">
      {!selected && <fieldset className="space-y-2"><legend>Connection method</legend>
        <label className="block"><input type="radio" name="jira-method" checked={method === 'forge_app'} disabled={!ready || !forgeAvailable || busy} onChange={() => { setMethod('forge_app'); setKey(''); }} /> Install ADP app</label>
        <p className="text-sm">Use the app's shared identity with automatically refreshed access. Product members do not supply Jira tokens.</p>
        {!forgeAvailable && <p role="status" className="text-sm">{ready ? 'App setup needed: an ADP platform administrator must publish and activate the Jira app for this deployment. You can use a service account below while app setup is pending.' : 'Checking whether app installation is available…'}</p>}
        <label className="block"><input type="radio" name="jira-method" checked={method === 'service_account'} disabled={!ready || busy} onChange={() => setMethod('service_account')} /> Connect service account</label>
        <p className="text-sm">Use an organization-managed account. An administrator supplies its token and replaces it before expiry.</p>
      </fieldset>}
      {method === 'forge_app' ? <p className="text-sm">A Jira administrator installs the ADP app and approves its permissions. Jira supplies refreshed app tokens automatically.</p> : <>
      <p className="text-sm">An Atlassian organization admin must enable API token access to Rovo MCP and create a scoped service account token. <a className="underline" href="https://developer.atlassian.com/cloud/rovo-mcp/guides/configuring-authentication-via-api-token/" target="_blank" rel="noreferrer">Atlassian setup guide</a></p>
      </>}
      <label className="block">Connection name<input required maxLength={200} value={label} onChange={e => setLabel(e.target.value)} className="ml-2 rounded border p-1 dark:bg-gray-900" /></label>
      {method === 'service_account' && <>
      <label className="block">Jira site ID<input required readOnly={!!selected} placeholder="Cloud ID (UUID)" value={site} onChange={e => setSite(e.target.value)} className="ml-2 rounded border p-1 dark:bg-gray-900" /></label>
      <label className="block">Service account token<input required type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} className="ml-2 rounded border p-1 dark:bg-gray-900" /></label>
      <p className="text-sm">ADP stores this token securely and does not display it again. Replace it here before it expires.</p>
      <label className="block">Token expiry date (UTC)<input required type="date" value={expiry} onChange={e => setExpiry(e.target.value)} className="ml-2 rounded border p-1 dark:bg-gray-900" /></label>
      </>}
      {productId ? <div>
        <p className="text-sm">{selected ? 'This connection is available to this Product. Its existing Product access will be preserved.' : 'This connection will be available to this Product only.'} Members and their agents can select any Jira project or board this identity can read. Grant the identity access only to information you intend to share.</p>
        {selected && selected.product_ids.length > 1 && <p className="text-sm">Replacing or reconnecting this identity also affects the other Products using it.</p>}
      </div> : <fieldset><legend>Products allowed to use this connection</legend>
        <p className="text-sm">Members of these Products can select any Jira project or board this identity can read. Grant it access only to the information you intend to share.</p>
        {products.map(product => <label className="block" key={product.id}><input type="checkbox" checked={allowed.includes(product.id)} onChange={e => setAllowed(current => e.target.checked ? [...current, product.id] : current.filter(id => id !== product.id))} /> {product.name}</label>)}
        {!products.length && <p>Create a Product before setting up shared Jira.</p>}
      </fieldset>}
      <button disabled={busy || !ready || !allowed.length} className="rounded border px-3 py-1" type="submit">{busy ? 'Verifying…' : method === 'forge_app' ? (selected?.status === 'ready' ? 'Save Product access' : 'Create connection code') : 'Verify and save'}</button>
      <button disabled={busy} type="button" className="ml-3 underline" onClick={() => { setEditing(false); setKey(''); }}>Cancel</button>
    </form>}
  </Wrapper>;
}
