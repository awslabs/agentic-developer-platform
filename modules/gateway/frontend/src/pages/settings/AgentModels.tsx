import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import {
  getManageableServicePrincipals,
  getModelCatalogue,
  getPersonaCatalogue,
  getPreferences,
  isPersonaModelConflict,
  personaModelErrorMessage,
  resetPreference,
  setPreference,
  type ManageableServicePrincipal,
  type ModelCatalogue,
  type ModelCatalogueRow,
  type PersonaCatalogueRow,
  type PersonaModelScope,
  type PersonaPreference,
  type PreferenceDetail,
} from '@/services/personaModels';

interface LoadedState {
  personas: PersonaCatalogueRow[];
  preferences: PersonaPreference[];
  catalogues: Record<string, ModelCatalogue>;
}

function permissionDenied(error: unknown): boolean {
  const detail = (error as { detail?: unknown })?.detail;
  return typeof detail === 'string' && /permission|human caller|administration requires/i.test(detail);
}

function availability(model: ModelCatalogueRow | undefined): { label: string; className: string } {
  if (!model) return { label: 'Availability unknown', className: 'text-gray-600' };
  if (model.invocable === true && !model.evidence?.stale) {
    return { label: 'Verified', className: 'text-green-700' };
  }
  if (model.invocable === false) return { label: 'Unavailable', className: 'text-red-700' };
  if (model.evidence?.stale) return { label: 'Evidence stale', className: 'text-amber-700' };
  return { label: 'Not yet certified', className: 'text-gray-600' };
}

function reasonLabel(reason: string | null): string {
  const labels: Record<string, string> = {
    probing_disabled: 'No certification probe has run.',
    not_yet_certified: 'No certification probe has run.',
    not_permitted: 'Not permitted by your organization.',
    not_invocable: 'A probe confirmed that this model cannot currently run.',
    evidence_stale: 'The last certification evidence has expired.',
    retired: 'This model is retired.',
    harness_incompatible: 'This model is incompatible with the persona harness.',
  };
  return (reason && labels[reason]) || 'This model cannot currently be selected.';
}

function priceLabel(model: ModelCatalogueRow): string | null {
  const price = model.price_context;
  if (!price || (price.input_per_million_tokens == null && price.output_per_million_tokens == null)) return null;
  const input = price.input_per_million_tokens == null ? 'unknown' : `$${price.input_per_million_tokens}`;
  const output = price.output_per_million_tokens == null ? 'unknown' : `$${price.output_per_million_tokens}`;
  return `${input} input / ${output} output per 1M tokens`;
}

function mergeDetail(entries: PersonaPreference[], detail: PreferenceDetail): PersonaPreference[] {
  return entries.map((entry) =>
    entry.persona_key === detail.persona_key
      ? {
          ...entry,
          effective_model_id: detail.effective_model_id,
          source: detail.source,
          status: detail.status as PersonaPreference['status'],
          saved_model_id: detail.saved_model_id,
          requested_alias: detail.requested_alias,
          revision: detail.revision,
          updated_at: detail.updated_at,
        }
      : entry,
  );
}

function PersonaCard({
  persona,
  preference,
  catalogue,
  busy,
  error,
  conflict,
  onSave,
  onReset,
  onReload,
}: {
  persona: PersonaCatalogueRow;
  preference?: PersonaPreference;
  catalogue?: ModelCatalogue;
  busy: boolean;
  error?: string;
  conflict: boolean;
  onSave: (model: string) => void;
  onReset: () => void;
  onReload: () => void;
}) {
  const [draft, setDraft] = useState(preference?.saved_model_id ?? '');
  useEffect(() => setDraft(preference?.saved_model_id ?? ''), [preference?.saved_model_id]);

  const effective = catalogue?.models.find((model) => model.canonical_model_id === preference?.effective_model_id);
  const state = availability(effective);
  const canSave = Boolean(
    persona.configurable &&
      draft &&
      draft !== preference?.saved_model_id &&
      catalogue?.models.some((model) => model.canonical_model_id === draft && model.selectable),
  );

  return (
    <article className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800" data-testid={`persona-row-${persona.key}`}>
      <div className="grid gap-4 md:grid-cols-[minmax(12rem,1.2fr)_minmax(12rem,1fr)_minmax(15rem,1.5fr)_auto] md:items-start">
        <div>
          <h2 className="font-semibold text-gray-900 dark:text-white">{persona.display_name}</h2>
          <p className="text-sm text-gray-600 dark:text-gray-300">{persona.purpose}</p>
          <p className="mt-1 text-xs text-gray-500">{persona.compatibility_class}</p>
        </div>

        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Effective model</p>
          <p className="break-words text-sm font-medium text-gray-900 dark:text-white">
            {effective ? `${effective.model_family} ${effective.canonical_version}` : preference?.effective_model_id || 'No effective model'}
          </p>
          <p className="text-xs text-gray-500">
            {preference?.source === 'principal-mapping' ? 'Personal mapping' : 'Platform default'}
          </p>
          <p className={`mt-2 text-sm font-medium ${state.className}`}>{state.label}</p>
          {effective?.harness_contract_revision && <p className="text-xs text-gray-500">Harness revision {effective.harness_contract_revision}</p>}
        </div>

        <div>
          {!persona.configurable ? (
            <p className="text-sm text-gray-600" data-testid={`not-configurable-${persona.key}`}>
              Not configurable{persona.not_configurable_reason ? ` — ${persona.not_configurable_reason.split('_').join(' ')}` : ''}.
            </p>
          ) : (
            <fieldset disabled={busy}>
              <legend className="text-xs font-medium uppercase tracking-wide text-gray-500">Saved model</legend>
              <p className="mb-2 text-xs text-gray-500">{preference?.saved_model_id || 'Not set'}</p>
              <div className="space-y-2" role="radiogroup" aria-label={`Model for ${persona.display_name}`}>
                {(catalogue?.models ?? []).map((model) => (
                  <label
                    key={model.canonical_model_id}
                    className={`flex cursor-pointer gap-2 rounded-md border p-2 text-sm ${model.selectable ? 'border-gray-200' : 'cursor-not-allowed border-gray-100 opacity-70'}`}
                  >
                    <input
                      type="radio"
                      name={`model-${persona.key}`}
                      value={model.canonical_model_id}
                      checked={draft === model.canonical_model_id}
                      disabled={!model.selectable}
                      onChange={() => setDraft(model.canonical_model_id)}
                    />
                    <span>
                      <span className="font-medium">{model.model_family} {model.canonical_version}</span>
                      <span className="block break-all text-xs text-gray-500">{model.canonical_model_id}</span>
                      {!model.selectable && <span className="block text-xs text-gray-600">{reasonLabel(model.reason)}</span>}
                      {priceLabel(model) && <span className="block text-xs text-gray-500">{priceLabel(model)}</span>}
                    </span>
                  </label>
                ))}
                {catalogue && catalogue.models.length === 0 && <p className="text-sm text-gray-600">No compatible models are published for this persona.</p>}
              </div>
            </fieldset>
          )}
        </div>

        {persona.configurable && (
          <div className="flex flex-wrap gap-2 md:flex-col">
            <Button size="sm" disabled={!canSave} isLoading={busy} onClick={() => onSave(draft)}>
              Save
            </Button>
            <Button size="sm" variant="outline" disabled={!preference?.saved_model_id || busy} onClick={onReset}>
              Reset
            </Button>
          </div>
        )}
      </div>

      {error && <p className="mt-3 text-sm text-red-700" role="alert">{error} Nothing was changed.</p>}
      {conflict && (
        <div className="mt-3 text-sm text-amber-800" role="alert">
          This mapping changed elsewhere. Your edit was not applied.{' '}
          <button type="button" className="font-medium underline" onClick={onReload}>Reload current value</button>
        </div>
      )}
    </article>
  );
}

export default function AgentModels() {
  const [scope, setScope] = useState<PersonaModelScope>({ kind: 'self' });
  const [principals, setPrincipals] = useState<ManageableServicePrincipal[]>([]);
  const [scopeLoadWarning, setScopeLoadWarning] = useState<string | null>(null);
  const [data, setData] = useState<LoadedState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyPersonas, setBusyPersonas] = useState<Record<string, boolean>>({});
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [conflicts, setConflicts] = useState<Record<string, boolean>>({});

  const load = useCallback(async (activeScope: PersonaModelScope, signal?: AbortSignal) => {
    setLoading(true);
    setLoadError(null);
    try {
      const [personaResponse, preferenceResponse] = await Promise.all([
        getPersonaCatalogue(signal),
        getPreferences(activeScope, signal),
      ]);
      const catalogues = await Promise.all(
        personaResponse.personas
          .filter((persona) => persona.configurable)
          .map((persona) => getModelCatalogue(activeScope, persona.key, signal)),
      );
      setData({
        personas: personaResponse.personas,
        preferences: preferenceResponse.entries,
        catalogues: Object.fromEntries(catalogues.map((catalogue) => [catalogue.persona_key, catalogue])),
      });
      setRowErrors({});
      setConflicts({});
    } catch (error) {
      if (signal?.aborted) return;
      setData(null);
      setLoadError(personaModelErrorMessage(error, 'Could not load Agent Models.'));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    getManageableServicePrincipals(controller.signal)
      .then((response) => setPrincipals(response.principals.filter((principal) => principal.manageable)))
      .catch((error) => {
        if (!controller.signal.aborted && !permissionDenied(error)) {
          setScopeLoadWarning('Managed service accounts could not be loaded. Your own mappings are still available.');
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(scope, controller.signal);
    return () => controller.abort();
  }, [load, scope]);

  const selectedPrincipal = useMemo(
    () => principals.find((principal) => principal.canonical_service_principal_id === scope.canonicalPrincipalId),
    [principals, scope.canonicalPrincipalId],
  );
  const nothingCertified = Boolean(
    data && Object.values(data.catalogues).every((catalogue) => catalogue.models.every((model) => !model.selectable)),
  );

  const applyMutation = (detail: PreferenceDetail) => {
    setData((current) => current && ({ ...current, preferences: mergeDetail(current.preferences, detail) }));
  };

  const save = async (persona: PersonaCatalogueRow, model: string) => {
    const preference = data?.preferences.find((entry) => entry.persona_key === persona.key);
    setBusyPersonas((current) => ({ ...current, [persona.key]: true }));
    setRowErrors((current) => ({ ...current, [persona.key]: '' }));
    setConflicts((current) => ({ ...current, [persona.key]: false }));
    try {
      applyMutation(await setPreference(scope, persona.key, model, preference?.revision ?? undefined));
    } catch (error) {
      if (isPersonaModelConflict(error)) {
        setConflicts((current) => ({ ...current, [persona.key]: true }));
      } else {
        setRowErrors((current) => ({ ...current, [persona.key]: personaModelErrorMessage(error, 'This mapping could not be saved.') }));
      }
    } finally {
      setBusyPersonas((current) => ({ ...current, [persona.key]: false }));
    }
  };

  const reset = async (persona: PersonaCatalogueRow) => {
    setBusyPersonas((current) => ({ ...current, [persona.key]: true }));
    setRowErrors((current) => ({ ...current, [persona.key]: '' }));
    setConflicts((current) => ({ ...current, [persona.key]: false }));
    try {
      applyMutation(await resetPreference(scope, persona.key));
    } catch (error) {
      setRowErrors((current) => ({ ...current, [persona.key]: personaModelErrorMessage(error, 'This mapping could not be reset.') }));
    } finally {
      setBusyPersonas((current) => ({ ...current, [persona.key]: false }));
    }
  };

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-4 py-6 sm:px-6" data-testid="agent-models-page">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Agent Models</h1>
        <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">Choose which certified model each agent persona uses.</p>
      </div>

      {principals.length > 0 && (
        <fieldset className="rounded-lg border border-gray-200 p-4">
          <legend className="px-1 text-sm font-medium">Configuration scope</legend>
          <div className="flex flex-wrap gap-3">
            <label className="flex items-center gap-2">
              <input type="radio" name="scope" checked={scope.kind === 'self'} onChange={() => setScope({ kind: 'self' })} />
              My own agents
            </label>
            {principals.map((principal) => (
              <label key={principal.canonical_service_principal_id} className="flex items-center gap-2">
                <input
                  type="radio"
                  name="scope"
                  checked={scope.kind === 'service' && scope.canonicalPrincipalId === principal.canonical_service_principal_id}
                  onChange={() => setScope({ kind: 'service', canonicalPrincipalId: principal.canonical_service_principal_id })}
                />
                {principal.display_name} ({principal.tenant_label}, {principal.source})
              </label>
            ))}
          </div>
        </fieldset>
      )}

      {selectedPrincipal && (
        <Alert variant="info" title="Managed service account">
          Changes below apply to {selectedPrincipal.display_name} in {selectedPrincipal.tenant_label}, not to your own account.
        </Alert>
      )}
      {scopeLoadWarning && <Alert variant="warning">{scopeLoadWarning}</Alert>}
      <Alert variant="info">A change applies to the next new agent chain. It does not change a run already in progress.</Alert>

      {loading && <div className="flex items-center gap-3"><Spinner /><span>Loading Agent Models…</span></div>}
      {!loading && loadError && (
        <Alert variant="error" title="Agent Models could not be loaded">
          <p>{loadError} This is not a statement that the platform default is in effect.</p>
          <Button className="mt-3" size="sm" variant="outline" onClick={() => load(scope)}>Retry</Button>
        </Alert>
      )}
      {!loading && data && (
        <>
          {nothingCertified && (
            <Alert variant="info" title="No model has been certified yet">
              Selections cannot be saved until invocability evidence exists. The effective models below remain the current policy answer.
            </Alert>
          )}
          <div className="space-y-4">
            {data.personas.map((persona) => (
              <PersonaCard
                key={persona.key}
                persona={persona}
                preference={data.preferences.find((entry) => entry.persona_key === persona.key)}
                catalogue={data.catalogues[persona.key]}
                busy={Boolean(busyPersonas[persona.key])}
                error={rowErrors[persona.key]}
                conflict={Boolean(conflicts[persona.key])}
                onSave={(model) => void save(persona, model)}
                onReset={() => void reset(persona)}
                onReload={() => void load(scope)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
