import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { addGitHubOrgMember, addTeamMember, getOrgTeams, getOrgUsers } from '@/services/admin';
import { getProductPeople, productReceipt, updateProductPeople, type ProductPeople as People, type ProductPerson } from '@/services/products';

type Props = { productId: string; projectId: string; orgId: string; userId: string; revision: number; onSaved: () => void; onPeople: (people: People | null) => void; embedded?: boolean };
type PersonOption = Awaited<ReturnType<typeof getOrgUsers>>['items'][number];
type Pending = { revision: number; receipt: string; patch: { team_id?: string | null; owner_user_id?: string } };
const label = (person: ProductPerson) => person.github_username ? `${person.name} (@${person.github_username})` : person.name;

export default function ProductPeople({ productId, projectId, orgId, userId, revision, onSaved, onPeople, embedded = false }: Props) {
  const storageKey = `product-people:${orgId}:${userId}:${productId}:${projectId}`;
  const [people, setPeople] = useState<People | null>(null);
  const [editing, setEditing] = useState(false);
  const [teams, setTeams] = useState<{ id: string; name: string }[]>([]);
  const [users, setUsers] = useState<PersonOption[]>([]);
  const [teamId, setTeamId] = useState('');
  const [ownerId, setOwnerId] = useState('');
  const [memberId, setMemberId] = useState('');
  const [github, setGithub] = useState('');
  const [method, setMethod] = useState<'member' | 'github'>('member');
  const [busy, setBusy] = useState(false);
  const [choicesReady, setChoicesReady] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [pending, setPending] = useState<Pending | null>(() => {
    try { return JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch { return null; }
  });
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const retain = (write: Pending | null) => {
    setPending(write);
    try { if (write) sessionStorage.setItem(storageKey, JSON.stringify(write)); else sessionStorage.removeItem(storageKey); } catch { /* The in-memory receipt remains available. */ }
  };
  const refresh = useCallback(async (offset = 0) => {
    const current = await getProductPeople(productId, projectId, offset);
    if (!mounted.current) return current;
    setPeople(previous => offset && previous && previous.team?.id === current.team?.id ? { ...current, members: [...previous.members, ...current.members] } : current);
    onPeople(current);
    return current;
  }, [productId, projectId, onPeople]);

  useEffect(() => {
    void refresh().catch(() => {
      if (mounted.current) { setPeople(null); onPeople(null); setError('People and access are unavailable. Refresh to try again.'); }
    });
  }, [revision, refresh, onPeople]);

  const manageTeam = people?.permissions.manage_team ?? false;
  const managePeople = people?.permissions.manage_people ?? false;
  useEffect(() => {
    if (!editing) return;
    let active = true;
    setChoicesReady(false);
    async function loadChoices() {
      const nextTeams: typeof teams = [], nextUsers: PersonOption[] = [];
      if (manageTeam) {
        for (let page = 1; ; page++) {
          const result = await getOrgTeams(orgId, { page, pageSize: 100 });
          nextTeams.push(...result.items);
          if (!result.hasMore) break;
        }
      }
      if (managePeople || manageTeam) {
        for (let page = 1; ; page++) {
          const result = await getOrgUsers(orgId, { page, pageSize: 100 });
          nextUsers.push(...result.items);
          if (!result.hasMore) break;
        }
      }
      if (active) { setTeams(nextTeams); setUsers(nextUsers); setChoicesReady(true); }
    }
    void loadChoices().catch(() => { if (active) setError('Could not load organization choices. Close and reopen Manage people to retry.'); });
    return () => { active = false; };
    // Choices belong to the editing session, not every polling readback.
  }, [editing, orgId, manageTeam, managePeople]);

  async function save(write: Pending) {
    if (busy) return;
    setBusy(true); setError(''); setMessage(''); retain(write);
    try {
      await updateProductPeople(productId, projectId, write.revision, write.patch, write.receipt);
      if (!mounted.current) return;
      retain(null);
      setEditing(false);
      setMessage('Product people updated.');
      onSaved();
      await refresh();
    } catch (failure) {
      if (!mounted.current) return;
      const status = (failure as { status?: number }).status;
      if (status && status >= 400 && status < 500) {
        retain(null);
        setEditing(false);
        setError(status === 412 ? 'Product changed elsewhere. Review the latest people and choose your changes again.' : 'This change is not available with your current access. Refresh people before trying again.');
        await refresh().catch(() => { setPeople(null); onPeople(null); });
      } else {
        setError('Could not confirm the save. Check save status before making another change.');
      }
    } finally { if (mounted.current) setBusy(false); }
  }

  async function addPerson() {
    if (!people?.team || busy) return;
    setBusy(true); setError(''); setMessage('');
    try {
      if (method === 'github') await addGitHubOrgMember(orgId, { github_username: github.trim().replace(/^@/, ''), team_id: people.team.id, role: 'member' });
      else await addTeamMember(orgId, people.team.id, { userId: memberId, role: 'member', isPrimary: false });
      if (!mounted.current) return;
      setGithub(''); setMemberId('');
      setMessage('Team membership saved.');
      await refresh(); onSaved();
    } catch {
      if (mounted.current) setError('Could not confirm team membership. Refresh the members below before retrying. Check that the person is eligible and you have organization access.');
    } finally { if (mounted.current) setBusy(false); }
  }

  const canManage = people?.permissions.manage_team || people?.permissions.manage_people;
  return <section aria-label="People and access" className={embedded ? 'product-profile-people space-y-4' : 'blueprint-card product-card space-y-4'}>
    <div className="flex flex-wrap items-center justify-between gap-3">{embedded ? <h3>People &amp; access</h3> : <h2>People &amp; access</h2>}
      {canManage && <button type="button" className="product-text-button" disabled={busy || !!pending} onClick={() => {
        setTeamId(people?.team?.id ?? ''); setOwnerId(people?.owner.id ?? ''); setError(''); setEditing(!editing);
      }}>{editing ? 'Close people editor' : 'Manage people'}</button>}
    </div>
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {pending && <button type="button" disabled={busy} onClick={() => void save(pending)}>Check people save status</button>}
    {people ? <>
      <dl className="grid gap-4 sm:grid-cols-2"><div><dt className="product-muted">Product owner</dt><dd className="mt-1 font-semibold">{label(people.owner)}</dd></div>
        <div><dt className="product-muted">ADP team</dt><dd className="mt-1 font-semibold">{people.team?.name ?? 'No team assigned'}</dd></div></dl>
      <p className="product-muted">Product team members and platform admins can access this product across all its delivery scopes.</p>
      {people.team && <div><h3 className="mb-2 font-semibold">Team members</h3>
        {people.members.length ? <ul className="flex flex-wrap gap-2">{people.members.map(person => <li className="product-person" key={person.id}>{label(person)}</li>)}</ul> : <p className="product-muted">No members in this team yet.</p>}
        {people.next_offset !== null && <button type="button" onClick={() => void refresh(people.next_offset!).catch(() => setError('Could not load more members.'))}>Load more members</button>}
      </div>}
      {!canManage && <p className="product-muted">An organization administrator can change the owner, assign a team, or add people.</p>}
      {editing && <div className="space-y-5 border-t border-gray-200 pt-4">
        {!choicesReady && <p>Loading organization choices…</p>}
        <form className="space-y-3" onSubmit={event => {
          event.preventDefault();
          const patch: Pending['patch'] = {};
          if (people.permissions.manage_team && teamId !== (people.team?.id ?? '')) patch.team_id = teamId || null;
          if (people.permissions.manage_people && ownerId !== people.owner.id) patch.owner_user_id = ownerId;
          if (!pending && Object.keys(patch).length) void save({ revision: people.revision, receipt: productReceipt(), patch });
        }}>
          {people.permissions.manage_team && <label>Product team<select value={teamId} disabled={busy || !!pending || !choicesReady} onChange={event => setTeamId(event.target.value)}>
            <option value="">No team (platform admins only)</option>{teams.map(team => <option key={team.id} value={team.id}>{team.name}</option>)}
          </select></label>}
          {people.permissions.manage_people && <label>Product owner<select value={ownerId} disabled={busy || !!pending || !choicesReady} onChange={event => setOwnerId(event.target.value)}>
            {!users.some(person => person.id === people.owner.id) && <option value={people.owner.id}>{label(people.owner)}</option>}
            {users.map(person => <option key={person.id} value={person.id}>{person.name || person.githubUsername || person.email}{person.githubUsername ? ` (@${person.githubUsername})` : ''}</option>)}
          </select></label>}
          <button className="product-primary" disabled={busy || !!pending || !choicesReady || (teamId === (people.team?.id ?? '') && ownerId === people.owner.id)}>Save people</button>
        </form>
        {people.permissions.manage_team && people.team && <form className="space-y-3" onSubmit={event => { event.preventDefault(); void addPerson(); }}>
          <h3 className="font-semibold">Add a person to {people.team.name}</h3>
          <p className="product-muted">Membership also grants access to other products shared with this team. A GitHub account will join your ADP organization as a member.</p>
          <label>Add using<select value={method} onChange={event => setMethod(event.target.value as typeof method)} disabled={busy || !!pending}>
            <option value="member">Existing ADP member</option>{people.permissions.enroll_github && <option value="github">GitHub username</option>}
          </select></label>
          {method === 'member' ? <label>Organization member<select value={memberId} onChange={event => setMemberId(event.target.value)} disabled={busy || !!pending || !choicesReady}>
            <option value="">Choose a person</option>{users.filter(person => !people.members.some(member => member.id === person.id)).map(person => <option key={person.id} value={person.id}>{person.name || person.githubUsername || person.email}{person.githubUsername ? ` (@${person.githubUsername})` : ''}</option>)}
          </select></label> : <label>GitHub username<input value={github} onChange={event => setGithub(event.target.value)} placeholder="octocat" maxLength={40} disabled={busy || !!pending} /></label>}
          <button disabled={busy || !!pending || !choicesReady || (method === 'member' ? !memberId : !github.trim())}>Add to team</button>
        </form>}
        <Link className="product-text-button" to="/admin/organizations">Manage organization teams</Link>
      </div>}
    </> : !error && <p>Loading people…</p>}
    <button type="button" className="product-text-button" disabled={busy} onClick={() => { setError(''); void refresh().catch(() => { setPeople(null); onPeople(null); setError('People and access are unavailable. Refresh to try again.'); }); }}>Refresh people</button>
  </section>;
}
