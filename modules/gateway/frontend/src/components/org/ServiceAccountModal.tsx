import { useEffect, useRef, useState } from 'react';
import { Button, Input, Modal, Select } from '@/components/ui';
import { createAgent, getAgentCredentials, updateAgent, type AgentCredentials } from '@/services/agents';
import { loadIdentityHierarchy, type OrganizationIdentityHierarchy, type OrganizationServiceIdentity } from '@/services/organizationServiceIdentities';

export function ServiceAccountModal({ orgId, identity, onSaved, onClose }: {
  orgId: string;
  identity?: OrganizationServiceIdentity;
  onSaved: (row: OrganizationServiceIdentity) => void;
  onClose: (needsRefresh?: boolean) => void;
}) {
  const [hierarchy, setHierarchy] = useState<OrganizationIdentityHierarchy>();
  const [hierarchyError, setHierarchyError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [department, setDepartment] = useState(identity?.departmentId || '');
  const [team, setTeam] = useState(identity?.teamId || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [createdId, setCreatedId] = useState('');
  const [showSecret, setShowSecret] = useState(false);
  const [credentials, setCredentials] = useState<AgentCredentials>();
  const [creationUncertain, setCreationUncertain] = useState(false);
  const active = useRef(true);
  const saving = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => {
    let current = true;
    setHierarchyError(false);
    void loadIdentityHierarchy(orgId).then(value => { if (current) setHierarchy(value); })
      .catch(() => { if (current) setHierarchyError(true); });
    return () => { current = false; };
  }, [orgId, attempt]);

  const valid = Boolean(hierarchy?.departments[department] && hierarchy?.teams[team]?.departmentId === department);
  async function reveal(clientId: string) {
    setBusy(true); setError('');
    try {
      const value = await getAgentCredentials(clientId, orgId);
      if (active.current) setCredentials(value);
    } catch {
      if (active.current) setError('The account was created, but its credentials could not be loaded. Retry loading credentials; do not create another account.');
    } finally { if (active.current) setBusy(false); }
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (saving.current || !valid || (!identity && !name.trim())) return;
    saving.current = true; setBusy(true); setError('');
    try {
      const agent = identity
        ? await updateAgent(identity.id, { department_id: department, team_id: team }, orgId)
        : await createAgent({ org_id: orgId, name: name.trim(), description: description.trim(), department_id: department, team_id: team, scopes: ['bedrockgw/invoke'] });
      if (!active.current) return;
      onSaved({ id: agent.client_id, source: 'cognito', name: agent.name, departmentId: agent.department_id, teamId: agent.team_id, status: agent.status });
      if (identity) { onClose(); return; }
      setCreatedId(agent.client_id);
      await reveal(agent.client_id);
    } catch {
      if (active.current) {
        setError(identity ? 'Assignment could not be saved. Please try again.' : 'Creation could not be confirmed. Close this dialog and check the account list before trying again.');
        if (!identity) setCreationUncertain(true);
      }
    } finally { saving.current = false; if (active.current) setBusy(false); }
  }
  return <Modal isOpen onClose={() => { if (!busy) onClose(creationUncertain); }} title={createdId ? 'Service account created' : identity ? 'Edit assignment' : 'Add service account'}>
    {createdId ? <div className="space-y-4">
      <p className="text-sm">Save these credentials securely before closing. This dialog clears them when closed.</p>
      <Input name="service-client-id" label="Client ID" readOnly value={createdId} />
      {credentials && <>
        <Input name="service-client-secret" label="Client secret" type={showSecret ? 'text' : 'password'} readOnly value={credentials.client_secret} autoComplete="off" />
        <Button variant="secondary" onClick={() => setShowSecret(value => !value)}>{showSecret ? 'Hide secret' : 'Show secret'}</Button>
        <Input name="service-token-endpoint" label="Token endpoint" readOnly value={credentials.token_endpoint} />
        <p className="text-sm">Allowed scope: bedrockgw/invoke</p>
      </>}
      {error && <p role="alert">{error}</p>}
      {!credentials && <Button disabled={busy} onClick={() => void reveal(createdId)}>Retry loading credentials</Button>}
      <Button disabled={busy} onClick={() => onClose(creationUncertain)}>Done</Button>
    </div> : <form onSubmit={submit} className="space-y-4">
      <Input name="service-organization" label="Organization" readOnly value={orgId} />
      {identity ? <p className="text-sm">{identity.name}</p> : <>
        <Input name="service-name" label="Name" value={name} onChange={e => setName(e.target.value)} maxLength={128} required disabled={busy} />
        <Input name="service-description" label="Description" value={description} onChange={e => setDescription(e.target.value)} maxLength={512} disabled={busy} />
      </>}
      <Select name="service-department" label="Department" value={department} required disabled={!hierarchy || busy} placeholder="Select a department"
        options={Object.entries(hierarchy?.departments || {}).map(([value, label]) => ({ value, label }))}
        onChange={e => { setDepartment(e.target.value); setTeam(''); }} />
      <Select name="service-team" label="Team" value={team} required disabled={!department || !hierarchy || busy} placeholder="Select a team"
        options={Object.entries(hierarchy?.teams || {}).filter(([, value]) => value.departmentId === department).map(([value, item]) => ({ value, label: item.name }))}
        onChange={e => setTeam(e.target.value)} />
      {!hierarchy && !hierarchyError && <p role="status">Loading departments and teams…</p>}
      {hierarchyError && <div role="alert">Departments and teams could not be loaded. <Button type="button" onClick={() => setAttempt(value => value + 1)}>Retry hierarchy</Button></div>}
      {hierarchy && (!Object.keys(hierarchy.departments).length || (department && !Object.values(hierarchy.teams).some(value => value.departmentId === department))) && <p>Create a department and team in Structure before assigning this account.</p>}
      <p className="text-sm text-gray-500">{identity ? 'Updated assignments apply to newly issued tokens. Existing tokens keep their claims until they expire.' : 'The account can invoke models using its organization, department and team policies.'}</p>
      {error && <p role="alert">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" disabled={busy} onClick={() => onClose(creationUncertain)}>Cancel</Button>
        <Button type="submit" disabled={busy || !valid || creationUncertain || (!identity && !name.trim())} isLoading={busy}>{identity ? 'Save assignment' : 'Create service account'}</Button>
      </div>
    </form>}
  </Modal>;
}
