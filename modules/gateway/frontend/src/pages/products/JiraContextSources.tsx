import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { AdminRole } from '@/types';
import { LineIcon } from '@/components/ui/LineIcon';
import { ProviderIcon } from '@/components/ui/ProviderIcon';
import JiraSystemConnection from '@/pages/settings/JiraSystemConnection';
import { jiraConnections, jiraResources, jiraSources, readJiraSource, selectJiraSource, unlinkJiraSource,
  type JiraConnection, type JiraContextResult, type JiraResource, type JiraSource, type JiraView } from '@/services/jiraContext';

export type JiraConnectionListState = { status: 'loading' | 'ready' | 'unavailable'; count: number };
type Props = { productId: string; projectId: string; onSaved: () => void; setup?: boolean; list?: boolean; onReadyChange?: (ready: boolean) => void; onListChange?: (state: JiraConnectionListState) => void };

export default function JiraContextSources({ productId, projectId, onSaved, setup = false, list = false, onReadyChange, onListChange }: Props) {
  const { hasRole } = useAuth();
  const isPlatformAdmin = hasRole(AdminRole.PLATFORM_ADMIN);
  const [connectionRevision, setConnectionRevision] = useState(0);
  const refreshConnections = useCallback(() => setConnectionRevision(current => current + 1), []);

  if (list) return <JiraSourceSelection key={`${productId}:${projectId}`} productId={productId} projectId={projectId} onSaved={onSaved} isPlatformAdmin={isPlatformAdmin} list onListChange={onListChange} />;

  return <div aria-label="Jira context" className="space-y-3 rounded border p-4">
    {!setup && <><h3 className="font-semibold">Jira context</h3>
    <p>Connect Jira projects and boards for live issue, backlog and sprint reads. Context is fetched when you or an agent asks for it.</p></>}
    {isPlatformAdmin && <JiraSystemConnection key={productId} productId={productId} onChanged={refreshConnections} initialSetup={setup} />}
    {!isPlatformAdmin && setup && <p role="status">Shared credentials require a platform administrator. You can select a Jira source below once an administrator has enabled a connection for this Product.</p>}
    <JiraSourceSelection key={`${productId}:${projectId}:${connectionRevision}`} productId={productId} projectId={projectId} onSaved={onSaved} isPlatformAdmin={isPlatformAdmin} onReadyChange={onReadyChange} />
  </div>;
}

function JiraSourceSelection({ productId, projectId, onSaved, isPlatformAdmin, onReadyChange, list = false, onListChange }: Props & { isPlatformAdmin: boolean }) {
  const active = useRef(true);
  const [connections, setConnections] = useState<JiraConnection[]>([]);
  const [sources, setSources] = useState<JiraSource[]>([]);
  const [connection, setConnection] = useState('');
  const [projects, setProjects] = useState<JiraResource[]>([]);
  const [boards, setBoards] = useState<JiraResource[]>([]);
  const [project, setProject] = useState('');
  const [board, setBoard] = useState('');
  const [projectOffset, setProjectOffset] = useState<number | null>(null);
  const [boardOffset, setBoardOffset] = useState<number | null>(null);
  const [search, setSearch] = useState('');
  const [share, setShare] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [available, setAvailable] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [preview, setPreview] = useState<JiraContextResult | null>(null);
  // Keep unavailable linked sources reachable so members can still remove them.
  const rows = [...connections];
  for (const source of sources) {
    if (!rows.some(row => row.id === source.connection_id)) rows.push({ id: source.connection_id, label: 'Unavailable Jira connection', site_id: source.site_id });
  }
  useEffect(() => { onListChange?.({ status: available ? 'ready' : loadFailed ? 'unavailable' : 'loading', count: rows.length }); }, [available, loadFailed, rows.length, onListChange]);
  useEffect(() => { onReadyChange?.(sources.some(source => source.available !== false)); }, [sources, onReadyChange]);

  useEffect(() => {
    active.current = true;
    let current = true;
    void Promise.all([jiraConnections(productId, projectId), jiraSources(productId, projectId)]).then(([choices, saved]) => {
      if (current) { setConnections(choices.connections); setSources(saved.sources); setAvailable(true); }
    }).catch(() => { if (current) { setLoadFailed(true); setMessage('Jira connections could not be loaded. Refresh sources to try again.'); } });
    return () => { current = false; active.current = false; };
  }, [productId, projectId]);

  async function action(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setMessage('');
    try { await work(); }
    catch { if (active.current) setMessage('Jira access or the source changed, or the read failed. Refresh the page and check the shared connection.'); }
    finally { if (active.current) setBusy(false); }
  }
  async function discover(id: string, kind: 'projects' | 'boards', query = '', offset = 0) {
    const reply = await jiraResources(productId, projectId, id, kind, query, offset);
    if (!active.current) return;
    if (!Array.isArray(reply.data?.values)) throw new Error('Jira resources unavailable');
    const rows = reply.data.values;
    const next = reply.data.isLast === false && rows.length ? offset + rows.length : null;
    if (kind === 'projects') { setProjects(current => offset ? [...current, ...rows] : rows); setProjectOffset(next); }
    else { setBoards(current => offset ? [...current, ...rows] : rows); setBoardOffset(next); }
  }
  function chooseConnection(id: string) {
    setConnection(id); setProject(''); setBoard(''); setProjects([]); setBoards([]); setShare(false);
    setPreview(null); setMessage(''); setSearch('');
    setProjectOffset(null); setBoardOffset(null);
    if (id && connections.some(item => item.id === id)) void action(() => discover(id, 'projects'));
  }
  function chooseProject(id: string) {
    setProject(id); setBoard(''); setBoards([]); setBoardOffset(null); setShare(false);
    if (id) void action(() => discover(connection, 'boards', id));
  }
  async function save() {
    const selected = projects.find(item => String(item.id) === project);
    if (!selected || !share) return;
    await selectJiraSource(productId, projectId, connection, selected, board ? Number(board) : undefined);
    const saved = await jiraSources(productId, projectId);
    if (active.current) { setSources(saved.sources); setShare(false); setMessage('Jira source connected. Agents can read its live context.'); onSaved(); }
  }
  async function previewSource(source: JiraSource, view: JiraView, cursor?: string) {
    setPreview(null);
    try {
      const result = await readJiraSource(productId, projectId, source, view, undefined, cursor);
      if (active.current) setPreview(result);
    } catch (error) {
      if (view !== 'sprints') throw error;
      if (active.current) setMessage('Sprints could not be read. This board may not support sprints, or its Jira permissions may need to be checked.');
    }
  }
  async function remove(source: JiraSource) {
    await unlinkJiraSource(productId, projectId, source);
    if (active.current) { setSources(current => current.filter(item => item.id !== source.id)); setPreview(null); onSaved(); }
  }
  const visibleSources = list ? sources.filter(source => source.connection_id === connection) : sources;
  const selection = <>
    {message && <p role="status">{message}</p>}
    {!list && available && !connections.length && <p>{isPlatformAdmin ? 'Set up shared Jira above, then choose a project and optional board here.' : 'Ask a platform admin to set up shared Jira here for this Product. You can then choose a Jira project and board without providing a token.'}</p>}
    {connections.length > 0 && (!list || connections.some(item => item.id === connection)) && <fieldset disabled={busy} className="space-y-2">
      {!list && <label className="block">Shared connection <select value={connection} onChange={e => chooseConnection(e.target.value)}><option value="">Choose a connection</option>{connections.map(c => <option key={c.id} value={c.id}>{c.label}</option>)}</select></label>}
      {connection && <>
        <form onSubmit={e => { e.preventDefault(); setProject(''); setBoard(''); setBoards([]); void action(() => discover(connection, 'projects', search)); }}>
          <label>Find Jira project <input maxLength={100} value={search} onChange={e => setSearch(e.target.value)} /></label> <button type="submit">Search</button>
        </form>
        <label className="block">Jira project <select value={project} onChange={e => chooseProject(e.target.value)}><option value="">Choose a project</option>{projects.map(p => <option key={p.id} value={String(p.id)}>{p.name} ({p.key})</option>)}</select></label>
        {projectOffset !== null && <button type="button" onClick={() => void action(() => discover(connection, 'projects', search, projectOffset))}>More Jira projects</button>}
      </>}
      {project && <>
        <label className="block">Board <select value={board} onChange={e => setBoard(e.target.value)}><option value="">All project issues</option>{boards.map(b => <option key={b.id} value={String(b.id)}>{b.name}</option>)}</select></label>
        {boardOffset !== null && <button type="button" onClick={() => void action(() => discover(connection, 'boards', project, boardOffset))}>More boards</button>}
        <label className="block"><input type="checkbox" checked={share} onChange={e => setShare(e.target.checked)} /> Share this source with this Product’s members and their agents</label>
        <button type="button" disabled={!share} onClick={() => void action(save)}>Connect Jira source</button>
      </>}
    </fieldset>}
    {busy && <p role="status">Reading Jira…</p>}
    {visibleSources.map(source => <div className={list ? 'product-source-resource' : 'rounded border p-3'} key={source.id}>
      <p>{source.label} ({source.jira_project_key}){source.board_id ? ` · Board ${source.board_id}` : ' · All issues'}</p>
      {(source.available === false || !connections.some(item => item.id === source.connection_id)) && <p>Shared connection unavailable. Ask an admin to check it, or remove this source.</p>}
      <div className="flex flex-wrap gap-3">
        {(['issues', ...(source.board_id ? ['backlog', 'board', ...(source.board_type === 'kanban' ? [] : ['sprints'])] : [])] as JiraView[]).map(view => <button type="button" className="underline" key={view} disabled={busy || source.available === false || !connections.some(item => item.id === source.connection_id)} onClick={() => void action(() => previewSource(source, view))}>Read {view === 'board' ? 'board configuration' : view}</button>)}
        <button type="button" className="underline" disabled={busy} onClick={() => void action(() => remove(source))}>Remove source</button>
      </div>
    </div>)}
    {preview && <div aria-label="Live Jira preview" className="rounded border p-3">
      <p>{preview.source.label} · {preview.view} · Read {new Date(preview.retrieved_at_ms).toLocaleString()}</p>
      {preview.data.issues?.map(issue => <article key={issue.id} className="mt-2">
        <p>{issue.webUrl?.startsWith('https://') ? <a href={issue.webUrl} target="_blank" rel="noreferrer" className="underline">{issue.key}</a> : issue.key}: {issue.fields?.summary} · {issue.fields?.status?.name}</p>
        {issue.fields?.description && <p className="whitespace-pre-wrap text-sm">{issue.fields.description}</p>}
      </article>)}
      {preview.data.values?.map(sprint => <p key={sprint.id}>{sprint.name} · {sprint.state}{sprint.goal ? ` · ${sprint.goal}` : ''}</p>)}
      {preview.view === 'board' && <p>{preview.data.name} · {preview.data.type}</p>}
      {preview.data.returnHint && <p>{preview.data.returnHint}</p>}
      {preview.data.issues?.length === 0 && <p>No issues returned for this source.</p>}
      {preview.next_cursor && <button type="button" disabled={busy} onClick={() => void action(() => previewSource(preview.source, preview.view, preview.next_cursor!))}>Next page</button>}
      <p className="text-sm">{preview.next_cursor ? 'More results are available.' : 'Showing the provider response for this request.'}</p>
    </div>}
  </>;
  if (!list) return selection;
  return <>
    {!available && !loadFailed && <p role="status">Checking Jira connections…</p>}
    {loadFailed && <p role="alert">{message}</p>}
    {rows.map(row => <div className="product-connection" key={row.id}>
      <button type="button" aria-label={`Browse ${row.label}`} aria-expanded={connection === row.id} disabled={busy}
        onClick={() => chooseConnection(connection === row.id ? '' : row.id)}>
        <span className="product-connection-identity">
          <span className="product-provider-icon"><ProviderIcon provider="jira" /></span>
          <span className="product-connection-copy"><span>{row.label}</span><span className="product-provider-label">Jira · {sources.filter(source => source.connection_id === row.id).length} linked</span></span>
        </span>
        <span className="flex shrink-0 items-center gap-2 whitespace-nowrap">Browse<LineIcon className="h-4 w-4 shrink-0"><path d="m9 5 7 7-7 7" /></LineIcon></span>
      </button>
      {connection === row.id && <div className="product-source-results space-y-3">
        <h4 className="font-semibold">Sources in {row.label}</h4>
        <p>Choose Jira projects and boards for live issues, backlogs and sprints.</p>
        {selection}
        {isPlatformAdmin && <a href="/settings/connections" className="underline">Manage Jira connection</a>}
      </div>}
    </div>)}
  </>;
}
