import { useEffect, useState } from 'react';
import { Alert, Button, Modal, Select } from '@/components/ui';
import { getOrganizations } from '@/services/admin';
import { linkAwsConnection, listExistingAwsConnections } from '@/services/bedrockRouting';
import { describeRoutingReason, type ExistingAwsConnection } from '@/types/bedrockRouting';

function errorMessage(error: unknown): string {
  const detail = (error as { detail?: { message?: string; reason?: string } | string })?.detail;
  if (typeof detail === 'string') return detail;
  return [describeRoutingReason(detail?.reason), detail?.message].filter(Boolean).join(' ') || (error as Error)?.message || 'The request failed. Please retry.';
}

export function LinkAwsConnectionModal({ onClose, onLinked }: { onClose: () => void; onLinked: (message: string) => void }) {
  const [connections, setConnections] = useState<ExistingAwsConnection[]>([]);
  const [orgs, setOrgs] = useState<Array<{ id: string; name: string }>>([]);
  const [credentialId, setCredentialId] = useState('');
  const [orgId, setOrgId] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    async function loadOrganizations() {
      const rows: Array<{ id: string; name: string }> = [];
      for (let page = 1; ; page += 1) {
        const result = await getOrganizations({ page, pageSize: 100 });
        if (cancelled) return rows;
        rows.push(...result.items);
        if (!result.hasMore) return rows;
      }
    }
    Promise.all([listExistingAwsConnections(), loadOrganizations()])
      .then(([accounts, organizations]) => {
        if (cancelled) return;
        setConnections(accounts);
        setOrgs(organizations);
      })
      .catch((err: unknown) => { if (!cancelled) setLoadError(errorMessage(err)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [attempt]);

  const selected = connections.find((connection) => connection.credential_id === credentialId);
  const close = () => { if (!saving) onClose(); };
  const submit = async () => {
    if (!selected?.selectable || !orgId || saving) return;
    setSaving(true);
    setError(null);
    try {
      await linkAwsConnection(credentialId, orgId);
      onLinked('AWS connection verified and linked. Add a team or organization routing rule to use this account.');
      onClose();
    } catch (err: unknown) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal isOpen onClose={close} title="Use existing AWS connection" size="lg">
      <div className="space-y-4">
        <p className="text-sm text-gray-600 dark:text-gray-400">
          Link an AWS connection to an organization for Bedrock calls. The connection keeps its current owner.
          You do not need AWS administrator access; ADP verifies the existing role before saving the link.
        </p>
        {loading && <p role="status">Loading AWS connections and organizations…</p>}
        {loadError && <Alert variant="error" title="Could not load connections or organizations">
          <p>{loadError}</p><Button variant="secondary" onClick={() => setAttempt((n) => n + 1)}>Retry</Button>
        </Alert>}
        {!loading && !loadError && <>
          {connections.length === 0 && <p>No AWS connections found. Ask the account owner to connect their account under Settings → Credentials.</p>}
          {orgs.length === 0 && <p>No organizations found. Create an organization before linking an account.</p>}
          <Select
            label="AWS connection" name="bedrock-link-connection" value={credentialId}
            onChange={(event) => { setCredentialId(event.target.value); setError(null); }}
            placeholder="Select an AWS connection" disabled={saving}
            options={connections.map((connection) => ({
              value: connection.credential_id,
              label: `${connection.label} (${connection.account_id || 'account pending'}) — ${connection.org_name} / ${connection.owner_name || connection.owner_scope} — ${connection.status}`,
            }))}
          />
          {selected && !selected.selectable && <Alert variant="warning" title="Connection needs verification">
            {describeRoutingReason(selected.reason)} Ask the connection owner to finish setup and verify it first.
          </Alert>}
          <Select
            label="Link to organization" name="bedrock-link-org" value={orgId}
            onChange={(event) => { setOrgId(event.target.value); setError(null); }}
            placeholder="Select an organization" disabled={saving}
            options={orgs.map((org) => ({ value: org.id, label: org.name || org.id }))}
          />
          <p className="text-sm text-gray-600 dark:text-gray-400">
            The role must allow shared Bedrock use. If verification fails, ask the AWS account administrator to update its trust policy or Bedrock permissions, then retry.
          </p>
        </>}
        {error && <Alert variant="error" title="Connection could not be linked">{error}</Alert>}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={close} disabled={saving}>Cancel</Button>
          <Button onClick={submit} disabled={loading || !!loadError || !selected?.selectable || !orgId || saving}>
            {saving ? 'Verifying…' : 'Verify & link'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
