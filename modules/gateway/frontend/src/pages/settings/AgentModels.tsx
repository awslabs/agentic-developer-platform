import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AgentTaskBudget } from '@/components/org/AgentTaskBudget';
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
    return { label: 'Retired', className: 'text-red-700 dark:text-red-300' };
  }
  if ((preference?.status === 'disallowed' || preference?.availability_status === 'disallowed') || model?.permitted === false || model?.reason === 'not_permitted') {
    return { label: 'Not permitted', className: 'text-red-700 dark:text-red-300' };
  }
  if ((preference?.status === 'stale' || preference?.availability_status === 'stale') || model?.reason === 'evidence_stale' || model?.evidence?.stale) {
    return { label: 'Availability needs checking', className: 'text-amber-700 dark:text-amber-300' };
  }
  if (preference?.availability_status === 'selectable' || (model?.selectable && model.invocable == null && !model.evidence)) {
    return { label: 'Available', className: 'text-green-700 dark:text-green-300' };
  }
  if (preference?.effective_is_candidate) return { label: 'Not ready', className: 'text-gray-600 dark:text-gray-300' };
  if ((preference?.status === 'unavailable' || preference?.availability_status === 'unavailable') || model?.reason === 'not_invocable' || model?.invocable === false) {
    return { label: 'Unavailable', className: 'text-red-700 dark:text-red-300' };
  }
  if (model?.reason === 'harness_incompatible') {
    return { label: 'Incompatible', className: 'text-red-700 dark:text-red-300' };
  }
  if (preference?.availability_status === 'unknown') return { label: 'Availability unknown', className: 'text-gray-600 dark:text-gray-300' };
  if (!model) return { label: 'Availability unknown', className: 'text-gray-600 dark:text-gray-300' };
  if (model.invocable === true && !model.evidence?.stale) {
    return { label: 'Available', className: 'text-green-700 dark:text-green-300' };
  }
  return { label: 'Not ready', className: 'text-gray-600 dark:text-gray-300' };
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

function modelName(model: ModelCatalogueRow): string {
  return `${model.model_family} ${model.canonical_version}`;
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
  const selected = catalogue?.models.find((model) => model.canonical_model_id === draft);
  const blockedModels = catalogue?.models.filter((model) => !model.selectable) ?? [];
  const state = availability(effective, preference);
  const effectivePrice = effective ? priceLabel(effective) : null;
  const selectedPrice = selected && selected.canonical_model_id !== preference?.effective_model_id
    ? priceLabel(selected)
    : null;
  const canSave = Boolean(
    persona.configurable &&
      draft &&
      draft !== preference?.saved_model_id &&
      selected?.selectable,
  );
  const missingDraft = draft && !selected ? draft : null;

  return (
    <article
      className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-800"
      data-testid={`persona-row-${persona.key}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-100 px-5 py-4 dark:border-gray-700 sm:px-6">
        <div className="min-w-0 flex-1">
          <h3 className="text-base font-semibold text-gray-900 dark:text-white">{persona.display_name}</h3>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">{persona.purpose}</p>
        </div>
        <span className={`rounded-full bg-gray-50 px-3 py-1 text-xs font-semibold dark:bg-gray-700 ${state.className}`}>
          {state.label}
        </span>
      </div>

      <div className="grid gap-5 px-5 py-5 sm:px-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <div className="min-w-0 rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-900/40">
          <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">Model for new runs</p>
          <p className="mt-2 break-words text-lg font-semibold text-gray-900 dark:text-white">
            {effective ? modelName(effective) : preference?.effective_model_id || 'No model configured'}
          </p>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-300" data-testid={`effective-source-${persona.key}`}>
            {effectiveSourceLabel(preference)}
          </p>
          {effectivePrice && <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">{effectivePrice}</p>}
          {preference?.warnings?.map((warning) => (
            <p key={warning} role="status" className="mt-3 text-sm text-amber-700 dark:text-amber-300">{warning}</p>
          ))}
        </div>

        <div className="min-w-0">
          {!persona.configurable ? (
            <p className="text-sm text-gray-600 dark:text-gray-300" data-testid={`not-configurable-${persona.key}`}>
              Model selection is not available for this persona.
            </p>
          ) : (
            <>
              {!catalogue ? (
                <div role="alert" className="text-sm text-amber-700 dark:text-amber-300">
                  <p>Could not load model choices. Your saved settings are still shown.</p>
                  <Button size="sm" variant="secondary" disabled={busy} onClick={onReload}>Reload models</Button>
                  {preference?.saved_model_id && (
                    <Button className="ml-2" size="sm" variant="outline" disabled={busy} onClick={onReset}>Use default</Button>
                  )}
                </div>
              ) : (
                <>
                  <label htmlFor={`model-${scopeIdentity}-${persona.key}`} className="block text-sm font-semibold text-gray-900 dark:text-white">
                    Change model
                  </label>
                  <select
                    id={`model-${scopeIdentity}-${persona.key}`}
                    value={draft}
                    disabled={busy || catalogue.models.length === 0}
                    onChange={(event) => setDraft(event.target.value)}
                    aria-label={`Model for ${persona.display_name}`}
                    className="mt-2 block w-full rounded-lg border border-gray-300 bg-white px-3 py-2.5 text-sm text-gray-900 shadow-sm focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-200 disabled:cursor-not-allowed disabled:bg-gray-100 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:focus:ring-primary-900"
                  >
                    <option value="">Select a model to override the default</option>
                    {missingDraft && <option value={missingDraft} disabled>{missingDraft} · No longer in the catalogue</option>}
                    {catalogue.models.map((model) => {
                      const duplicate = catalogue.models.some((other) =>
                        other.canonical_model_id !== model.canonical_model_id && modelName(other) === modelName(model));
                      return (
                        <option key={model.canonical_model_id} value={model.canonical_model_id} disabled={!model.selectable}>
                          {modelName(model)}{duplicate ? ` · ${model.canonical_model_id}` : ''}{!model.selectable ? ' · Unavailable' : ''}
                        </option>
                      );
                    })}
                  </select>
                  {selectedPrice && <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">{selectedPrice}</p>}
                  {catalogue.models.length === 0 && (
                    <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">No compatible models are published for this persona.</p>
                  )}
                  <div className="mt-4 flex flex-wrap items-center gap-2">
                    <Button size="sm" disabled={!canSave} isLoading={busy} onClick={() => onSave(draft)}>
                      Save model
                    </Button>
                    <Button size="sm" variant="outline" disabled={!preference?.saved_model_id || busy} onClick={onReset}>
                      Use default
                    </Button>
                  </div>
                  {blockedModels.length > 0 && (
                    <details className="mt-4 border-t border-gray-100 pt-3 text-sm dark:border-gray-700">
                      <summary className="cursor-pointer text-primary-700 hover:underline dark:text-primary-300">
                        {blockedModels.length} unavailable {blockedModels.length === 1 ? 'model' : 'models'}
                      </summary>
                      <ul className="mt-3 space-y-2">
                        {blockedModels.map((model) => (
                          <li key={model.canonical_model_id} className="rounded-md bg-gray-50 px-3 py-2 dark:bg-gray-900/40">
                            <span className="font-medium text-gray-800 dark:text-gray-200">{modelName(model)}</span>
                            {catalogue.models.some((other) =>
                              other.canonical_model_id !== model.canonical_model_id && modelName(other) === modelName(model)) && (
                              <span className="block break-all text-xs text-gray-500 dark:text-gray-400">{model.canonical_model_id}</span>
                            )}
                            <span className="block text-xs text-gray-600 dark:text-gray-400">{reasonLabel(model.reason)}</span>
                            {priceLabel(model) && <span className="block text-xs text-gray-500 dark:text-gray-400">{priceLabel(model)}</span>}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </>
              )}
            </>
          )}
        </div>
      </div>

      {error && <p className="mx-5 mb-4 text-sm text-red-700 dark:text-red-300" role="alert">{error}</p>}
      {conflict && (
        <div className="mx-5 mb-4 text-sm text-amber-800 dark:text-amber-300" role="alert">
          This mapping changed elsewhere. Your edit was not applied.{' '}
          <button type="button" className="font-medium underline" onClick={onReload}>Reload current value</button>
        </div>
      )}
      {persona.key.startsWith('agent-task-') && <AgentTaskBudget
        principal={scopeIdentity.startsWith('service:') ? scopeIdentity.slice(8) : undefined}
        persona={persona.key} selectionRevision={preference?.revision} model={selected?.canonical_model_id ?? preference?.effective_model_id} />}
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
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Agent Models</h1>
          <p className="mt-1 text-sm text-gray-600 dark:text-gray-300">Choose the model each agent persona uses when you start a new run.</p>
        </div>
        {principals.length > 0 && (
          <div className="w-full sm:w-72">
            <label htmlFor="agent-models-account" className="block text-sm font-semibold text-gray-900 dark:text-white">
              Settings for
            </label>
            <select
              id="agent-models-account"
              value={scopeKind === 'self' ? 'self' : adminPrincipalId}
              disabled={mutationInFlight}
              onChange={(event) => event.target.value === 'self'
                ? switchToSelf()
                : switchToAdmin(event.target.value)}
              className="mt-2 block w-full rounded-lg border border-gray-300 bg-white px-3 py-2.5 text-sm text-gray-900 shadow-sm focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-200 disabled:cursor-not-allowed disabled:bg-gray-100 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:focus:ring-primary-900"
            >
              <option value="self">My own agents</option>
              {principals.map((principal) => (
                <option key={principal.canonical_service_principal_id} value={principal.canonical_service_principal_id}>
                  {principal.display_name} ({principal.tenant_label})
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

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
            <div className="flex flex-wrap items-end justify-between gap-2">
              <div>
                <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Agent personas</h2>
                <p className="text-sm text-gray-600 dark:text-gray-300">Review the model in use, then change individual personas as needed.</p>
              </div>
              <span className="text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">{data.personas.length} personas</span>
            </div>
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
