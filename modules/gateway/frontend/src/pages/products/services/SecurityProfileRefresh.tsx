import { useEffect, useRef, useState } from 'react';
import { getProduct, productReceipt, type ProductProfile } from '@/services/products';
import { matchesSecurityScope, refreshSecurityProfile, type SecurityConfiguration, type Versioned } from '@/services/l5Services';

interface Props {
  productId: string; projectId: string; configuration: Versioned<SecurityConfiguration>;
  disabled: boolean; dirty: boolean; onBusy: (busy: boolean) => void;
  onApplied: () => Promise<void>; onUncertain: () => void; onAccessDenied: () => void;
}
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
export default function SecurityProfileRefresh({ productId, projectId, configuration, disabled, dirty, onBusy, onApplied, onUncertain, onAccessDenied }: Props) {
  const [profile, setProfile] = useState<ProductProfile | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  const savedRef = configuration.value.settings.profile_ref;
  async function read() {
    if (disabled || busy) return;
    const request = ++generation.current;
    setBusy(true); setError(''); setProfile(null); setReviewed(false);
    try {
      const current = await getProduct(productId, projectId);
      if (request !== generation.current) return;
      if (current.tenant_id !== configuration.value.scope.tenant_id || current.product_id !== productId
        || current.project_id !== projectId || !Number.isInteger(current.revision) || current.revision < 1) throw { status: 403 };
      setProfile(current);
    } catch (cause) {
      if (request !== generation.current) return;
      if ([403, 404].includes(Number(object(cause).status))) onAccessDenied();
      setError('The current Product profile could not be read. Read it again before applying any changes.');
    } finally { if (request === generation.current) setBusy(false); }
  }
  async function apply() {
    if (disabled || dirty || busy || !profile || !reviewed || !savedRef) return;
    const request = ++generation.current;
    setBusy(true); onBusy(true); setError('');
    try {
      const saved = await refreshSecurityProfile(productId, projectId, { ...savedRef, record_revision: String(profile.revision) }, configuration.etag, productReceipt());
      if (request !== generation.current) return;
      if (!matchesSecurityScope(saved.value.scope, configuration.value.scope, productId, projectId)
        || saved.value.settings.profile_ref.record_revision !== String(profile.revision)) throw { status: 403 };
      // Reload all saved fields and binding readiness; do not merge the old draft.
      await onApplied();
    } catch (cause) {
      if (request !== generation.current) return;
      onUncertain();
      if ([403, 404].includes(Number(object(cause).status))) onAccessDenied();
      setError('Profile refresh could not be confirmed. Reload Security configuration and review its saved Profile revision before making another change.');
    } finally { if (request === generation.current) { setBusy(false); onBusy(false); } }
  }
  const current = object(profile?.configuration);
  const sources = object(current.basic);
  return <aside aria-label="Update Security from Product profile">
    <h3>Product profile updates</h3>
    <p>Reloading Security reads its saved setup. Applying a newer Product profile updates inherited values and retains explicit overrides. New sources require separate selection; existing schedules keep their pinned sources. Review setup and renew owner consent after changes.</p>
    {dirty && <p>Save or reload your Security edits before applying a Product profile update.</p>}
    <button type="button" disabled={disabled || busy} onClick={() => void read()}>Read current Product profile</button>
    {error && <p role="alert">{error}</p>}
    {profile && <>
      <p>Saved Profile revision {savedRef.record_revision}; current Product revision {profile.revision}.</p>
      <dl><dt>Current Product purpose</dt><dd>{profile.product.purpose || 'Not provided'}</dd>
        <dt>Current data classification</dt><dd>{String(object(current.governance).data_classification ?? 'Not provided')}</dd></dl>
      <details><summary>Current Product source revisions</summary><ul>{['documents', 'repositories'].flatMap(field => Array.isArray(sources[field]) ? sources[field] as unknown[] : []).map((source, index) => {
        const ref = object(source); return <li key={index}>{String(ref.record_type ?? '')} {String(ref.record_id ?? '')} (revision {String(ref.record_revision ?? '')})</li>;
      })}</ul></details>
      <label className="block"><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} disabled={disabled || busy || dirty} /> I reviewed this Product revision and the effect on inherited Security setup</label>
      <button type="button" onClick={() => void apply()} disabled={disabled || busy || dirty || !reviewed || String(profile.revision) === savedRef.record_revision}>Apply reviewed Product profile</button>
    </>}
  </aside>;
}
