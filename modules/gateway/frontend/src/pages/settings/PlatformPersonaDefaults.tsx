import { useEffect, useState } from 'react';
import { apiClient } from '@/services/api';
import { personaModelErrorMessage } from '@/services/personaModels';
import { Button } from '@/components/ui/Button';

interface DefaultEntry {
  persona_key: string;
  display_name: string;
  compatibility_class: string;
  canonical_model_id: string | null;
  inherited_model_id: string | null;
  recommended_model_id: string | null;
  revision: number;
}
interface Defaults {
  entries: DefaultEntry[];
  models: Record<string, { id: string; label: string }[]>;
}

export function PlatformPersonaDefaults({ onSaved }: { onSaved: () => void }) {
  const [data, setData] = useState<Defaults | null>(null);
  const [error, setError] = useState('');
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    apiClient.get<Defaults>('/admin/persona-defaults', controller.signal)
      .then(setData)
      .catch((err) => {
        if (!controller.signal.aborted) setError(personaModelErrorMessage(err, 'Could not load platform defaults.'));
      });
    return () => controller.abort();
  }, [generation]);
  return <details className="rounded-xl border border-gray-200 p-5 dark:border-gray-700">
    <summary className="cursor-pointer font-semibold">Platform persona defaults</summary>
    <p className="my-3 text-sm">Choose defaults for new runs across the platform. Users can override each persona in their own settings.</p>
    {error && <p role="alert">{error}</p>}
    <Button variant="outline" size="sm" onClick={() => { setError(''); setGeneration((n) => n + 1); }}>Reload defaults</Button>
    {data?.entries.map((entry) => <DefaultRow key={`${entry.persona_key}:${entry.revision}`}
      entry={entry} models={data.models[entry.compatibility_class] ?? []}
      onSaved={(updated) => {
        setData((current) => current && ({ ...current, entries: current.entries.map((row) => row.persona_key === updated.persona_key ? updated : row) }));
        onSaved();
      }} />)}
  </details>;
}

function DefaultRow({ entry, models, onSaved }: {
  entry: DefaultEntry;
  models: { id: string; label: string }[];
  onSaved: (entry: DefaultEntry) => void;
}) {
  const [model, setModel] = useState(entry.canonical_model_id ?? '');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const save = async () => {
    setBusy(true);
    setError('');
    try {
      const updated = await apiClient.put<DefaultEntry>(`/admin/persona-defaults/${encodeURIComponent(entry.persona_key)}`, {
        canonical_model_id: model || null,
        expected_revision: entry.revision,
        reason: reason.trim(),
        operation_id: crypto.randomUUID(),
      });
      onSaved(updated);
    } catch (err) {
      setError(personaModelErrorMessage(err, 'Could not save this default. Reload before retrying.'));
    } finally {
      setBusy(false);
    }
  };
  return <div className="mt-4 space-y-2 border-t border-gray-200 pt-4 dark:border-gray-700">
    <label className="block font-medium" htmlFor={`platform-default-${entry.persona_key}`}>{entry.display_name}</label>
    <select id={`platform-default-${entry.persona_key}`} value={model} disabled={busy}
      className="w-full rounded border border-gray-300 bg-white p-2 dark:bg-gray-900"
      onChange={(event) => setModel(event.target.value)}>
      <option value="">Use SDK default{entry.inherited_model_id ? ` (${entry.inherited_model_id})` : ' (unset)'}</option>
      {entry.canonical_model_id && !models.some((row) => row.id === entry.canonical_model_id) &&
        <option value={entry.canonical_model_id} disabled>{entry.canonical_model_id} (unavailable)</option>}
      {models.map((row) => <option key={row.id} value={row.id}>{row.label}{row.id === entry.recommended_model_id ? ' (suggested)' : ''}</option>)}
    </select>
    <input aria-label={`Reason for ${entry.display_name} default`} placeholder="Reason for change" maxLength={512}
      className="w-full rounded border border-gray-300 bg-white p-2 dark:bg-gray-900"
      value={reason} disabled={busy} onChange={(event) => setReason(event.target.value)} />
    <Button size="sm" isLoading={busy} disabled={busy || !reason.trim() || model === (entry.canonical_model_id ?? '')}
      onClick={() => void save()}>Save platform default</Button>
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
  </div>;
}
