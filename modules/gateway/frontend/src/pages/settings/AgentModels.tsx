import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import {
  isPersonaModelConflict,
  personaModelErrorMessage,
  type ManageableServicePrincipal,
  type ModelCatalogue,
  type ModelCatalogueRow,
  type PersonaCatalogueRow,
  type PersonaPreference,
  type PreferenceDetail,
} from '@/services/personaModels';
import * as selfApi from '@/services/personaModelsSelf';
import * as adminApi from '@/services/personaModelsAdmin';

type ScopeKind = 'self' | 'service';

interface LoadedState {
  personas: PersonaCatalogueRow[];
  preferences: PersonaPreference[];
  catalogues: Record<string, ModelCatalogue>;
}

function permissionDenied(error: unknown): boolean {
  const value = error as { error?: unknown; reason?: unknown; detail?: unknown } | null;
  if (value?.error === 'access_denied' || value?.reason === 'access_denied') return true;
  const detail = value?.detail;
  if (detail && typeof detail === 'object') {
    const structured = detail as { error?: unknown; reason?: unknown };
    if (structured.error === 'access_denied' || structured.reason === 'access_denied') return true;
  }
  return typeof detail === 'string' && /permission|human caller|administration requires/i.test(detail);
}

function availability(
  model: ModelCatalogueRow | undefined,
  preference?: PersonaPreference,
): { label: string; className: string } {
  if (preference?.model_lifecycle === 'retired' || model?.retired || model?.reason === 'retired') {
    return { label: 'Retired', className: 'text-red-700' };
  }
  if ((preference?.status === 'disallowed' || preference?.availability_status === 'disallowed') || model?.permitted === false || model?.reason === 'not_permitted') {
    return { label: 'Not permitted', className: 'text-red-700' };
  }
  if ((preference?.status === 'stale' || preference?.availability_status === 'stale') || model?.reason === 'evidence_stale' || model?.evidence?.stale) {
    return { label: 'Availability needs checking', className: 'text-amber-700' };
  }
  if (preference?.availability_status === 'selectable' || (model?.selectable && model.invocable == null && !model.evidence)) {
    return { label: 'Available to select', className: 'text-green-700' };
  }
  if (preference?.effective_is_candidate) return { label: 'Not ready', className: 'text-gray-600' };
  if ((preference?.status === 'unavailable' || preference?.availability_status === 'unavailable') || model?.reason === 'not_invocable' || model?.invocable === false) {
    return { label: 'Unavailable', className: 'text-red-700' };
  }
  if (model?.reason === 'harness_incompatible') {
    return { label: 'Incompatible', className: 'text-red-700' };
  }
  if (preference?.availability_status === 'unknown') return { label: 'Availability unknown', className: 'text-gray-600' };
  if (!model) return { label: 'Availability unknown', className: 'text-gray-600' };
  if (model.invocable === true && !model.evidence?.stale) {
    return { label: 'Available', className: 'text-green-700' };
  }
  return { label: 'Not ready', className: 'text-gray-600' };
}

/**
 * Describe where the effective model came from, using the server's own proof state.
 *
 * A saved mapping is the person's choice, and the class default's proof state says
 * nothing about it. Otherwise the class default is reported as exactly what the
 * server recorded: proven, candidate, or absent. `effective_is_candidate` cannot
 * stand in for this, because it is false both for a proven default and for a class
 * with no default at all — which is how an absent default came to render as "proven".
 */
function effectiveSourceLabel(preference: PersonaPreference | undefined): string {
  if (!preference) return 'Source unknown';
  if (preference.source === 'principal-mapping') return 'Your choice';

  const status = preference.class_default_status;
  if (preference.availability_status === 'selectable' && preference.effective_model_id) return 'Default for this persona';
  if (status === 'proven') return 'Default for this persona';
  if (status === 'candidate') return 'Default for this persona (not ready)';
  if (preference.effective_model_id) {
    // A default model is in effect but the server did not state its proof state.
    // Report the uncertainty rather than upgrading it to proven.
    return 'Default for this persona (availability unconfirmed)';
  }
  return 'No default is configured for this persona';
}

function reasonLabel(reason: string | null): string {
  const labels: Record<string, string> = {
    probing_disabled: 'This model is not ready to use yet.',
    not_yet_certified: 'This model is not ready to use yet.',
    not_permitted: 'Not permitted by your organization.',
    not_invocable: 'This model is currently unavailable.',
    evidence_stale: 'Availability needs to be checked before this model can be selected.',
    retired: 'This model is retired.',
    harness_incompatible: 'This persona does not support this model.',
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
          model_lifecycle: detail.model_lifecycle,
          availability_status: detail.availability_status,
          availability_reason: detail.availability_reason,
          warnings: detail.warnings,
          effective_model_id: detail.effective_model_id,
          compatibility_class: detail.compatibility_class,
          harness_contract_revision: detail.harness_contract_revision,
          effective_is_candidate: detail.effective_is_candidate,
          source: detail.source,
          status: detail.status as PersonaPreference['status'],
          // Carried from the response so a reset back to the class default reports the
          // server's current proof state rather than the pre-mutation value.
          class_default_status: detail.class_default_status ?? null,
          saved_model_id: detail.saved_model_id,
          requested_alias: detail.requested_alias,
          revision: detail.revision,
          updated_at: detail.updated_at,
        }
      : entry,
  );
}

function PersonaCard({
  scopeIdentity,
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
  scopeIdentity: string;
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
  useEffect(
    () => setDraft(preference?.saved_model_id ?? ''),
    [preference?.saved_model_id, scopeIdentity],
  );

  const effective = catalogue?.models.find((model) => model.canonical_model_id === preference?.effective_model_id);
  const state = availability(effective, preference);
  const effectivePrice = effective ? priceLabel(effective) : null;
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
        </div>

        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-gray-500">Model for new runs</p>
          <p className="break-words text-sm font-medium text-gray-900 dark:text-white">
            {effective ? `${effective.model_family} ${effective.canonical_version}` : preference?.effective_model_id || 'No model configured'}
          </p>
          <p className="text-xs text-gray-500" data-testid={`effective-source-${persona.key}`}>
            {effectiveSourceLabel(preference)}
          </p>
          <p className={`mt-2 text-sm font-medium ${state.className}`}>{state.label}</p>
          {preference?.warnings?.map((warning) => <p key={warning} role="status" className="text-sm text-amber-700">{warning}</p>)}
          {effectivePrice && <p className="text-xs text-gray-500">{effectivePrice}</p>}
        </div>

        <div>
          {!persona.configurable ? (
            <p className="text-sm text-gray-600" data-testid={`not-configurable-${persona.key}`}>
              Model selection is not available for this persona.
            </p>
          ) : (
            <fieldset disabled={busy}>
              <legend className="text-xs font-medium uppercase tracking-wide text-gray-500">Choose a model</legend>
              {!catalogue && <div role="alert" className="text-sm text-amber-700">
                <p>Could not load model choices. Your saved settings are still shown.</p>
                <Button size="sm" variant="secondary" onClick={onReload}>Reload models</Button>
              </div>}
              <div className="space-y-2" role="radiogroup" aria-label={`Model for ${persona.display_name}`}>
                {(catalogue?.models ?? []).map((model) => (
                  <label
                    key={model.canonical_model_id}
                    className={`flex cursor-pointer gap-2 rounded-md border p-2 text-sm ${model.selectable ? 'border-gray-200' : 'cursor-not-allowed border-gray-100 opacity-70'}`}
                  >
                    <input
                      type="radio"
                      name={`model-${scopeIdentity}-${persona.key}`}
                      value={model.canonical_model_id}
                      checked={draft === model.canonical_model_id}
                      disabled={!model.selectable}
                      onChange={() => setDraft(model.canonical_model_id)}
                    />
                    <span>
                      <span className="font-medium">{model.model_family} {model.canonical_version}</span>
                      {catalogue!.models.some((other) => other.canonical_model_id !== model.canonical_model_id &&
                        other.model_family === model.model_family && other.canonical_version === model.canonical_version) && (
                        <span className="block break-all text-xs text-gray-500">{model.canonical_model_id}</span>
                      )}
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

      {error && <p className="mt-3 text-sm text-red-700" role="alert">{error}</p>}
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
  const [scopeKind, setScopeKind] = useState<ScopeKind>('self');
  const [adminPrincipalId, setAdminPrincipalId] = useState<string | undefined>(undefined);
  const [principals, setPrincipals] = useState<ManageableServicePrincipal[]>([]);
  const [scopeLoadWarning, setScopeLoadWarning] = useState<string | null>(null);
  const [data, setData] = useState<LoadedState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyPersonas, setBusyPersonas] = useState<Record<string, boolean>>({});
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [conflicts, setConflicts] = useState<Record<string, boolean>>({});
  const loadGeneration = useRef(0);
  const loadController = useRef<AbortController | null>(null);

  const load = useCallback(async (activeKind: ScopeKind, activePrincipalId?: string) => {
    const generation = loadGeneration.current + 1;
    loadGeneration.current = generation;
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    const isCurrent = () => !controller.signal.aborted && loadGeneration.current === generation;
    setLoading(true);
    setLoadError(null);
    try {
      const [personaResponse, preferenceResponse] = await Promise.all([
        selfApi.getPersonaCatalogue(controller.signal),
        activeKind === 'self'
          ? selfApi.getPreferences(controller.signal)
          : adminApi.getPreferences(activePrincipalId!, controller.signal),
      ]);
      const catalogueResults = await Promise.allSettled(
        personaResponse.personas.map((persona) =>
          activeKind === 'self'
            ? selfApi.getModelCatalogue(persona.key, controller.signal)
            : adminApi.getModelCatalogue(activePrincipalId!, persona.key, controller.signal),
        ),
      );
      if (!isCurrent()) return;
      const catalogues = catalogueResults.flatMap((result) => result.status === 'fulfilled' ? [result.value] : []);
      setData({
        personas: personaResponse.personas,
        preferences: preferenceResponse.entries,
        catalogues: Object.fromEntries(catalogues.map((catalogue) => [catalogue.persona_key, catalogue])),
      });
      setRowErrors({});
      setConflicts({});
    } catch (error) {
      if (!isCurrent()) return;
      setData(null);
      setLoadError(personaModelErrorMessage(error, 'Could not load Agent Models.'));
    } finally {
      if (isCurrent()) {
        setLoading(false);
        if (loadController.current === controller) loadController.current = null;
      }
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    selfApi.getManageableServicePrincipals(controller.signal)
      .then((response) => setPrincipals(response.principals.filter((principal) => principal.manageable)))
      .catch((error) => {
        if (!controller.signal.aborted && !permissionDenied(error)) {
          setScopeLoadWarning('Managed service accounts could not be loaded. Your own mappings are still available.');
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    void load(scopeKind, adminPrincipalId);
    return () => {
      loadGeneration.current += 1;
      loadController.current?.abort();
      loadController.current = null;
    };
  }, [load, scopeKind, adminPrincipalId]);

  const selectedPrincipal = useMemo(
    () => principals.find((principal) => principal.canonical_service_principal_id === adminPrincipalId),
    [principals, adminPrincipalId],
  );
  const scopeIdentity = scopeKind === 'self' ? 'self' : `service:${adminPrincipalId}`;
  const mutationInFlight = Object.values(busyPersonas).some(Boolean);
  const nothingCertified = useMemo(() => {
    if (!data) return false;
    const eligible = Object.values(data.catalogues)
      .flatMap((catalogue) => catalogue.models)
      .filter((model) => !model.retired && model.permitted !== false && model.reason !== 'harness_incompatible');
    return eligible.length > 0 && eligible.every((model) =>
      model.reason === 'probing_disabled' || model.reason === 'not_yet_certified');
  }, [data]);

  const applyMutation = (detail: PreferenceDetail) => {
    setData((current) => current && ({ ...current, preferences: mergeDetail(current.preferences, detail) }));
  };

  const save = async (persona: PersonaCatalogueRow, model: string) => {
    const preference = data?.preferences.find((entry) => entry.persona_key === persona.key);
    setBusyPersonas((current) => ({ ...current, [persona.key]: true }));
    setRowErrors((current) => ({ ...current, [persona.key]: '' }));
    setConflicts((current) => ({ ...current, [persona.key]: false }));
    try {
      const result = scopeKind === 'self'
        ? await selfApi.setPreference(persona.key, model, preference?.revision ?? undefined)
        : await adminApi.setPreference(adminPrincipalId!, persona.key, model, preference?.revision ?? undefined);
      applyMutation(result);
    } catch (error) {
      if (isPersonaModelConflict(error)) {
        setConflicts((current) => ({ ...current, [persona.key]: true }));
      } else {
        setRowErrors((current) => ({ ...current, [persona.key]: personaModelErrorMessage(error, 'Could not confirm this change. Reload your settings before trying again.') }));
      }
    } finally {
      setBusyPersonas((current) => ({ ...current, [persona.key]: false }));
    }
  };

  const reset = async (persona: PersonaCatalogueRow) => {
    const preference = data?.preferences.find((entry) => entry.persona_key === persona.key);
    if (!preference?.saved_model_id || preference.revision == null) {
      setRowErrors((current) => ({
        ...current,
        [persona.key]: 'The current mapping revision is unavailable. Reload before resetting.',
      }));
      return;
    }
    setBusyPersonas((current) => ({ ...current, [persona.key]: true }));
    setRowErrors((current) => ({ ...current, [persona.key]: '' }));
    setConflicts((current) => ({ ...current, [persona.key]: false }));
    try {
      const result = scopeKind === 'self'
        ? await selfApi.resetPreference(persona.key, preference.revision)
        : await adminApi.resetPreference(adminPrincipalId!, persona.key, preference.revision);
      applyMutation(result);
    } catch (error) {
      if (isPersonaModelConflict(error)) {
        setConflicts((current) => ({ ...current, [persona.key]: true }));
      } else {
        setRowErrors((current) => ({ ...current, [persona.key]: personaModelErrorMessage(error, 'Could not confirm the reset. Reload your settings before trying again.') }));
      }
    } finally {
      setBusyPersonas((current) => ({ ...current, [persona.key]: false }));
    }
  };

  const switchToSelf = () => {
    setScopeKind('self');
    setAdminPrincipalId(undefined);
  };

  const switchToAdmin = (principalId: string) => {
    setScopeKind('service');
    setAdminPrincipalId(principalId);
  };

  return (
    <div className="mx-auto max-w-6xl space-y-6 px-4 py-6 sm:px-6" data-testid="agent-models-page">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Agent Models</h1>
        <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">Choose the model each agent persona uses when you start a new run.</p>
      </div>

      {principals.length > 0 && (
        <fieldset disabled={mutationInFlight} className="rounded-lg border border-gray-200 p-4">
          <legend className="px-1 text-sm font-medium">Settings for</legend>
          <div className="flex flex-wrap gap-3">
            <label className="flex items-center gap-2">
              <input type="radio" name="scope" checked={scopeKind === 'self'} onChange={switchToSelf} />
              My own agents
            </label>
            {principals.map((principal) => (
              <label key={principal.canonical_service_principal_id} className="flex items-center gap-2">
                <input
                  type="radio"
                  name="scope"
                  checked={scopeKind === 'service' && adminPrincipalId === principal.canonical_service_principal_id}
                  onChange={() => switchToAdmin(principal.canonical_service_principal_id)}
                />
                {principal.display_name} ({principal.tenant_label})
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
      <Alert variant="info">Changes apply to new runs and the agents they start. Runs already in progress keep their settings.</Alert>

      {loading && <div className="flex items-center gap-3"><Spinner /><span>Loading Agent Models…</span></div>}
      {!loading && loadError && (
        <Alert variant="error" title="Agent Models could not be loaded">
          <p>{loadError}</p>
          <Button className="mt-3" size="sm" variant="outline" onClick={() => load(scopeKind, adminPrincipalId)}>Retry</Button>
        </Alert>
      )}
      {!loading && data && (
        <>
          {nothingCertified && (
            <Alert variant="info" title="Model choices are not ready yet">
              An ADP administrator needs to make models available before you can choose one. Your saved settings are shown below.
            </Alert>
          )}
          <div className="space-y-4">
            {data.personas.map((persona) => (
              <PersonaCard
                key={`${scopeIdentity}:${persona.key}`}
                scopeIdentity={scopeIdentity}
                persona={persona}
                preference={data.preferences.find((entry) => entry.persona_key === persona.key)}
                catalogue={data.catalogues[persona.key]}
                busy={Boolean(busyPersonas[persona.key])}
                error={rowErrors[persona.key]}
                conflict={Boolean(conflicts[persona.key])}
                onSave={(model) => void save(persona, model)}
                onReset={() => void reset(persona)}
                onReload={() => void load(scopeKind, adminPrincipalId)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
