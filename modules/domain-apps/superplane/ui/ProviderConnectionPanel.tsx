/**
 * Provider connection management for a workspace — #5730 AC-03.
 *
 * WHAT A "CONNECTION" IS HERE, AND WHY THE SECRET NEVER APPEARS
 * ------------------------------------------------------------
 * A provider connection binds a *vault credential reference* to a workspace. The
 * secret itself is entered through ADP's vault, which is its system of record.
 * This panel therefore has no secret input, no "paste your key" field, and no
 * state that could hold a value: it lists references by label and sends an id.
 *
 * That is a structural guarantee rather than a careful habit. `CredentialRef` has
 * no field for a value, `parseCredentialRef` drops everything it does not name,
 * and the bind request body is built only from named reference fields. There is no path down which
 * a secret could arrive, so none of the usual leaks — a value in component state,
 * in a serialized error, in a DevTools snapshot, in a future analytics payload —
 * has a source to draw from. The tests assert the absence rather than trusting it.
 *
 * WHY THERE IS NO LIST OF EXISTING CONNECTIONS
 * -------------------------------------------
 * The domain API serves register, get-by-id, rotate and revoke, but no
 * "list connections for this workspace" route. A connection can therefore only be
 * shown once this session has its id — either because it was just bound here, or
 * because the id was looked up. Rather than fake an inventory (or infer one from
 * the workspace row, which does not carry it), the panel says plainly that it can
 * only show connections bound in this session. Pretending to a complete inventory
 * would be the more damaging choice: an admin would read an empty list as "no
 * credentials are attached" and bind a duplicate.
 *
 * Gateway performs provider validation and attests the report. This panel never
 * invents readings or treats a successful bind as evidence of provider health.
 *
 * WHY THE PANEL REPORTS ITS READINGS UPWARDS
 * -----------------------------------------
 * The readiness panel above it used to be handed a literal `null` for the provider
 * reading, so it said "not validated by the service yet" while this panel was
 * showing the four readings it had just read off the server. One screen, two
 * answers about one credential, and the wrong one was the one dressed as a
 * readiness verdict. `onObservation` exists to make that impossible: whatever this
 * panel knows about the connection, the readiness report is derived from the same
 * fact. It reports observations only — never a verdict — because turning three
 * readings into "ready" is `readiness.ts`'s job and doing it in two places is how
 * the two come to disagree.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { Alert, Button, Select, Spinner } from '@/components/ui';

import {
  delegateCredential,
  validateConnection,
  ScopeGuard,
  buildBindBody,
  getConnection,
  isSuperseded,
  listCredentials,
  registerConnection,
  revokeConnection,
  type Outcome,
} from './client';
import {
  type CredentialRef,
  type ProviderConnection,
  type Unavailable,
  type ValidationReading,
} from './contract';
import { freshnessOf, useFreshnessClock, type ProviderObservation } from './readiness';

/**
 * What the panel is currently doing, as one value.
 *
 * A single state rather than several booleans, because the combinations the
 * booleans permit are mostly nonsense — validating while revoking, bound and
 * binding at once. Making the impossible states unrepresentable removes the class
 * of bug where two in-flight actions render a contradictory screen.
 */
type PanelState =
  | { name: 'idle' }
  | { name: 'loading-credentials' }
  | { name: 'binding' }
  | { name: 'refreshing' }
  | { name: 'revoking' };

export interface ProviderConnectionPanelProps {
  workspaceId: string;
  /** Provider families this environment says it supports, from the capability report. */
  providers: readonly string[];
  /** Whether this user may change bindings. Read-only users still see state. */
  mayManage: boolean;
  /** Set when the capability report itself could not be read. */
  capabilityUnavailable?: Unavailable | null;
  /**
   * Called whenever what is known about this workspace's connection changes.
   *
   * Observations, not verdicts — see the header comment. Fires with `null` readings
   * too, because "we bound a connection and it has no reading yet" and "we have not
   * looked" are different facts and the readiness report needs to be able to tell
   * them apart.
   */
  onObservation?: (observation: ProviderObservation) => void;
}

export function ProviderConnectionPanel({
  workspaceId,
  providers,
  mayManage,
  capabilityUnavailable = null,
  onObservation,
}: ProviderConnectionPanelProps) {
  const [state, setState] = useState<PanelState>({ name: 'idle' });
  const [credentials, setCredentials] = useState<CredentialRef[] | null>(null);
  const [credentialId, setCredentialId] = useState('');
  const [connection, setConnection] = useState<ProviderConnection | null>(null);
  const [validation, setValidation] = useState<ValidationReading | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const [revoked, setRevoked] = useState(false);
  const now = useFreshnessClock();
  const fresh = validation !== null && freshnessOf(validation.checked_at, now) === 'fresh';

  /**
   * The observation callback, held in a ref so it is not an effect dependency.
   *
   * A parent that rebuilds the callback each render would otherwise make the effect
   * below re-run on every render, and since it calls back into the parent's state
   * that is an infinite loop. Keeping the identity out of the dependency list means
   * the effect fires when the *observation* changes, which is what it is for.
   */
  const notify = useRef(onObservation);
  notify.current = onObservation;

  /**
   * Report upwards whatever is currently known about this workspace's connection.
   *
   * Deliberately derived from the panel's own state rather than sent from inside
   * each action: `bind`, `refresh` and `revoke` all change what is known, and a
   * call in each is three places to forget one. Reporting `admits_new_work` as
   * `null` when there is no connection is the honest reading — no connection is not
   * a connection that refuses work.
   */
  useEffect(() => {
    notify.current?.({
      workspaceId,
      validation,
      admitsNewWork: connection ? connection.admits_new_work : null,
    });
  }, [workspaceId, connection, validation]);

  // The selected row itself, which is what gets bound. Resolved once here so the
  // button's enabled state and the submitted body cannot disagree about whether a
  // real credential is selected.
  const selectedCredential = credentials?.find((c) => c.credential_id === credentialId) ?? null;

  // One guard per panel instance. Its generation is what makes a reply that
  // arrives after the user moved on discardable instead of applied.
  const [guard] = useState(() => new ScopeGuard());
  useEffect(() => () => guard.supersede(), [guard]);

  const loadCredentials = useCallback(async () => {
    setState({ name: 'loading-credentials' });
    setProblem(null);
    const outcome: Outcome<CredentialRef[]> = await listCredentials(guard);
    if (isSuperseded(outcome)) return;
    setState({ name: 'idle' });
    if (!outcome.ok) {
      if ('unavailable' in outcome) setProblem(outcome.unavailable);
      return;
    }
    setCredentials(outcome.value);
  }, [guard]);

  const bind = useCallback(async () => {
    // Bind the selected ROW, not a retyped id. The server requires the full
    // three-field reference and additionally requires `service` to equal
    // `provider`, so the body is built in one place from one row
    // ({@link buildBindBody}) rather than assembled from separate inputs that
    // can disagree. Guarded rather than assumed: the button is disabled without a
    // selection, but a submit can still arrive from a keyboard path.
    const selected = selectedCredential;
    if (!selected) return;
    setState({ name: 'binding' });
    setProblem(null);
    setRevoked(false);
    const generation = guard.current();
    const delegated = await delegateCredential(guard, workspaceId, selected.credential_id);
    if (isSuperseded(delegated) || !guard.isCurrent(generation)) return;
    if (!delegated.ok) {
      setState({ name: 'idle' });
      if ('unavailable' in delegated) setProblem(delegated.unavailable);
      return;
    }
    const outcome = await registerConnection(guard, workspaceId, buildBindBody(selected));
    if (isSuperseded(outcome)) return;
    setState({ name: 'idle' });
    if (!outcome.ok) {
      if ('unavailable' in outcome) setProblem(outcome.unavailable);
      return;
    }
    setConnection(outcome.value);
    // Deliberately not set from the bind response: a successful bind is not a
    // validation, and seeding a reading here would show a credential as working
    // before anything checked it. The server's own reading, if it sent one, is
    // read off the connection below.
    setValidation(outcome.value.validation ?? null);
  }, [guard, selectedCredential, workspaceId]);

  /**
   * Re-read the connection to pick up any reading the attesting service has filed.
   *
   * This is the honest substitute for a "check it now" button. It cannot cause a
   * check and does not claim to: it is a GET of a served route
   * (`getConnection`), so whatever readings come back were established by the
   * service, not by this click. If none have been filed the screen still says
   * "not validated" — refreshing does not convert absence of evidence into a pass.
   */
  const refresh = useCallback(async () => {
    if (!connection) return;
    setState({ name: 'refreshing' });
    setProblem(null);
    const outcome = await getConnection(guard, workspaceId, connection.connection_id);
    if (isSuperseded(outcome)) return;
    setState({ name: 'idle' });
    if (!outcome.ok) {
      if ('unavailable' in outcome) setProblem(outcome.unavailable);
      // The previously displayed reading is dropped: a failed read is not evidence
      // that the last reading still holds, and a stale "valid" outliving its
      // evidence is the dangerous direction.
      setValidation(null);
      return;
    }
    setConnection(outcome.value);
    setValidation(outcome.value.validation ?? null);
  }, [connection, guard, workspaceId]);

  const validate = useCallback(async () => {
    if (!connection) return;
    setState({ name: 'refreshing' });
    setProblem(null);
    setValidation(null);
    const outcome = await validateConnection(guard, workspaceId, connection.connection_id);
    if (isSuperseded(outcome)) return;
    setState({ name: 'idle' });
    if (!outcome.ok) {
      if ('unavailable' in outcome) setProblem(outcome.unavailable);
      return;
    }
    setConnection(outcome.value);
    setValidation(outcome.value.validation ?? null);
  }, [connection, guard, workspaceId]);

  const revoke = useCallback(async () => {
    if (!connection) return;
    setState({ name: 'revoking' });
    setProblem(null);
    const outcome = await revokeConnection(guard, workspaceId, connection.connection_id);
    if (isSuperseded(outcome)) return;
    setState({ name: 'idle' });
    if (!outcome.ok) {
      if ('unavailable' in outcome) setProblem(outcome.unavailable);
      return;
    }
    // Cleared together. Leaving the readings up after a revoke would show
    // validity for a binding that no longer exists.
    setConnection(null);
    setValidation(null);
    setRevoked(true);
  }, [connection, guard, workspaceId]);

  const busy = state.name !== 'idle';

  return (
    <section
      aria-labelledby="superplane-connections-heading"
      className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800"
    >
      <h2
        id="superplane-connections-heading"
        className="text-lg font-semibold text-gray-900 dark:text-white"
      >
        Provider connection
      </h2>
      <p className="mt-2 max-w-prose text-sm text-gray-600 dark:text-gray-300">
        A connection attaches a credential from your ADP vault to this workspace. Secret
        values are entered in the vault and are never shown or sent from this screen.
      </p>

      {capabilityUnavailable && (
        <div className="mt-4">
          <Alert variant="warning" title="Supported providers are not known yet">
            {capabilityUnavailable.detail} Any provider family may be entered, but this
            environment has not confirmed which ones it accepts.
          </Alert>
        </div>
      )}

      {!mayManage ? (
        <p className="mt-4 text-sm text-gray-600 dark:text-gray-300">
          Changing provider connections requires an organization or platform administrator.
        </p>
      ) : (
        <div className="mt-4 space-y-4">
          {credentials === null ? (
            <div>
              <Button variant="secondary" disabled={busy} onClick={() => void loadCredentials()}>
                Choose a vault credential
              </Button>
              {state.name === 'loading-credentials' && (
                <div className="mt-3 flex items-center gap-2">
                  <Spinner />
                  <span className="text-sm text-gray-600 dark:text-gray-400">
                    Reading vault credentials…
                  </span>
                </div>
              )}
            </div>
          ) : credentials.length === 0 ? (
            <Alert variant="info" title="No vault credentials available">
              Add a provider credential to your ADP vault first; it will then be selectable
              here by its label.
            </Alert>
          ) : (
            <>
              <Select
                id="superplane-credential"
                label="Vault credential"
                value={credentialId}
                onChange={(event) => setCredentialId(event.target.value)}
                options={[
                  { value: '', label: 'Select a credential' },
                  // Label and service only. The option values are ids, which is
                  // all that is ever submitted.
                  ...credentials.map((credential) => ({
                    value: credential.credential_id,
                    label: credential.label
                      ? `${credential.label} (${credential.service})`
                      : credential.credential_id,
                  })),
                ]}
              />
              {/*
                Provider is DERIVED from the chosen credential, not picked
                separately. The server requires the connection's provider to equal
                the credential's own service
                (`credential_service != provider` -> CredentialProviderMismatch),
                so two independent pickers let a user build a combination that is
                always refused, and the refusal names neither field. Showing the
                credential's provider read-only makes the mismatch unrepresentable.
              */}
              {selectedCredential && (
                <p className="text-sm text-gray-600 dark:text-gray-400">
                  Provider: <span className="font-medium">{selectedCredential.service}</span>
                  {!providers.includes(selectedCredential.service) && providers.length > 0 && (
                    <span className="ml-2 text-amber-700 dark:text-amber-400">
                      This environment does not list {selectedCredential.service} as a
                      supported provider family.
                    </span>
                  )}
                </p>
              )}
              <Button disabled={busy || !selectedCredential} onClick={() => void bind()}>
                Bind credential to workspace
              </Button>
              {state.name === 'binding' && (
                <div className="flex items-center gap-2">
                  <Spinner />
                  <span className="text-sm text-gray-600 dark:text-gray-400">Binding…</span>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {problem && (
        <div className="mt-4">
          <Alert variant="error" title={titleFor(problem)}>
            {problem.detail}
          </Alert>
        </div>
      )}

      {revoked && (
        <div className="mt-4">
          <Alert variant="success" title="Connection revoked">
            The credential is no longer bound to this workspace. The credential itself is
            untouched in your vault.
          </Alert>
        </div>
      )}

      {connection && (
        <div className="mt-6 border-t border-gray-200 pt-4 dark:border-gray-700">
          <h3 className="text-base font-semibold text-gray-900 dark:text-white">
            Bound credential
          </h3>
          <dl className="mt-3 space-y-2 text-sm">
            <Row term="Provider" value={connection.provider} />
            <Row
              term="Credential"
              value={connection.credential.label || connection.credential.credential_id}
            />
            <Row term="Status" value={connection.status} />
            {/* Rendered as a separate fact from validation, because they answer
                different questions: a connection can exist and admit no work. */}
            <Row
              term="Admits new work"
              value={fresh ? (connection.admits_new_work ? 'Yes' : 'No, or not reported') : 'Unknown; refresh validation'}
            />
          </dl>

          {connection.limitation && (
            <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">
              {connection.limitation}
            </p>
          )}

          <ValidationReadings reading={fresh ? validation : null} pending={state.name === 'refreshing'} />
          {validation && !fresh && <p>The last validation is stale. Request a fresh provider reading.</p>}

          <div className="mt-4 flex flex-wrap gap-3">
            {mayManage && (
              <Button disabled={busy} onClick={() => void validate()}>Validate credential</Button>
            )}
            <Button variant="secondary" disabled={busy} onClick={() => void refresh()}>
              Check for a new reading
            </Button>
            {mayManage && (
              <Button variant="danger" disabled={busy} onClick={() => void revoke()}>
                Revoke connection
              </Button>
            )}
          </div>
        </div>
      )}

      {/* Stated rather than hidden: see the header comment. An empty area here
          would read as "nothing is bound", which this screen cannot know. */}
      {!connection && !revoked && (
        <p className="mt-4 text-xs text-gray-500 dark:text-gray-400">
          Connections bound in an earlier session are not listed here — this environment's API
          does not offer a way to enumerate them. Readiness for the selected workspace is
          reported separately below.
        </p>
      )}
    </section>
  );
}

function Row({ term, value }: { term: string; value: string }) {
  return (
    <div className="flex gap-2">
      <dt className="w-40 shrink-0 text-gray-500 dark:text-gray-400">{term}</dt>
      <dd className="break-words text-gray-900 dark:text-white">{value}</dd>
    </div>
  );
}

/**
 * The three provider readings, separately.
 *
 * `null` is rendered as "not reported" and never as a failure. A provider that
 * did not answer a quota question has not answered it; showing that as "no quota"
 * would invent a refusal, and showing it as "ok" would invent an approval.
 */
function ValidationReadings({
  reading,
  pending,
}: {
  reading: ValidationReading | null;
  pending: boolean;
}) {
  if (pending) {
    return (
      <div className="mt-4 flex items-center gap-2">
        <Spinner />
        <span className="text-sm text-gray-600 dark:text-gray-400">
          Looking for a reading from the service…
        </span>
      </div>
    );
  }
  if (!reading) {
    return (
      <p className="mt-4 text-sm text-gray-600 dark:text-gray-300">
        This credential has not been validated by the service yet.
      </p>
    );
  }
  return (
    <dl className="mt-4 space-y-2 text-sm">
      <Row term="Credential valid" value={triState(reading.credential_valid)} />
      <Row term="Permissions sufficient" value={triState(reading.permissions_sufficient)} />
      <Row term="Quota available" value={triState(reading.quota_available)} />
      {reading.observed_capacity !== null && (
        <Row term="Observed capacity" value={String(reading.observed_capacity)} />
      )}
      {reading.checked_at && <Row term="Checked at" value={reading.checked_at} />}
      {reading.detail && <Row term="Detail" value={reading.detail} />}
    </dl>
  );
}

/** Three outcomes, three words. `null` is its own answer, not a synonym for no. */
function triState(value: boolean | null): string {
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  return 'Not reported';
}

function titleFor(unavailable: Unavailable): string {
  switch (unavailable.reason) {
    case 'not-permitted':
      return 'You do not have access to this';
    case 'not-deployed':
      return 'Not available in this environment';
    case 'unreachable':
      return 'Could not reach Superplane';
    default:
      return 'Something went wrong';
  }
}

export default ProviderConnectionPanel;
