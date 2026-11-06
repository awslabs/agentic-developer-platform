/**
 * useDevBox — Issue #8094.
 *
 * The one ensure/poll/open-dashboard flow for the owner's canonical EC2 Dev Box,
 * shared by the chat action (`components/chat/OpenMyCrewBox`) and the Settings
 * page (`pages/settings/DevBox`) so the two cannot drift. Everything here talks to
 * the admission-only owner API in `modules/gateway/src/cloud_desktop/routes.py`:
 *
 *   GET  /cloud-desktops                      owned boxes (contract_version 1)
 *   GET  /cloud-desktops/policy               { admission_allowed, reason }
 *   GET  /cloud-desktops/crew-template        { template } (403 when EC2 Crew is not permitted)
 *   POST /cloud-desktops/ensure               { template, profile } + Idempotency-Key → EnsureResponse
 *   GET  /cloud-desktops/{id}                 desktop read (status/observed/desired, lease, generation, crew_bootstrap)
 *   GET  /cloud-desktops/{id}/operations/{op} OperationResponse { status, progress, error_class }
 *   GET  /cloud-desktops/{id}/resources       retained disks / snapshots
 *   POST /cloud-desktops/{id}/{start|stop|replace|release} + Idempotency-Key (release needs workspace_retention)
 *   GET  /crew/target                          verified Crew lease for the current host generation
 *
 * It never calls `/ssh-access`: that route mints short-lived credentials for the
 * CLI's ProxyCommand only, and nothing in the browser should ever hold them.
 *
 * Host enrollment (#8140) runs automatically inside `openBox()` ONCE per machine generation: it needs no owner input,
 * only the owner's browser (the device key is generated here and sealed to the box), so a
 * running box whose generation no browser has approved (`host_binding === 'none'`) is enrolled through the headless
 * `services/crewHostEnrollment.enrollCrewHost` before readiness is awaited. After a stop/start the
 * controller re-binds the host run itself; the browser only waits. A pending
 * enrollment is awaited, never re-submitted; refusals surface by name.
 *
 * `openBox()` runs in the interstitial tab (`pages/settings/DevBoxOpen`) that the Dev Box page
 * and the chat action open synchronously on the click (`services/devBoxOpen`); the page and
 * the action themselves only watch (`refresh`) while that tab works.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';
import { resolveCrewDashboardDestination } from '@/services/crewDashboard';
import { enrollCrewHost } from '@/services/crewHostEnrollment';

export type DevBoxCost = {
  estimated_cost_usd: string;
  reconciled_cost_usd: string | null;
  total_cost_usd: string | null;
  coverage: string;
  retained_resources: Array<{ kind: string; size_gb: number | null }>;
  by_kind?: { snapshot?: { known_cost_usd: string; storage_gb: number | null; rate_usd_per_gb_hour: string | null } };
};

export type DevBox = {
  desktop_id: string;
  name?: string | null;
  profile: string;
  template: string | null;
  template_revision?: number | null;
  status: string;
  observed_state: string;
  desired_state?: string;
  machine_generation: number;
  lease_expires_at?: string | null;
  /** "image" (baked AMI) or "user-data" (installed at first boot) — #8055. */
  crew_bootstrap?: 'image' | 'user-data' | string;
  /** Controller-verified Crew readiness when the desktop read carries it (#8056). */
  crew_ready?: boolean;
  /** On-box bootstrap receipt (/run/adp/crew-bootstrap.json) when the desktop read carries it; shape may grow. */
  crew_bootstrap_receipt?: { status?: string; profile_revision?: string; error?: string | null } | null;
  /** Controller-named reason the running host refuses readiness (`host_refused:<Class>`), else null (#8223). */
  host_readiness_refusal?: string | null;
  /**
   * Where this generation's host stands with the browser and the controller (#8223 simplification):
   * `none` — no device approved yet, the browser enrolls once; `pending_enrollment` — approval awaiting
   * the controller; `enrolled` — approval consumed, the controller owns the binding and re-binds a run
   * after every start by itself; `active` — a live host run exists.
   */
  host_binding?: 'none' | 'pending_enrollment' | 'enrolled' | 'active' | string;
  infrastructure_cost: DevBoxCost;
};

export type DevBoxOperation = {
  desktop_id: string;
  operation_id: string | null;
  status: string;
  progress: string;
  error_class?: string | null;
};

export type DevBoxPolicy = { contract_version: number; admission_allowed: boolean; reason: string | null };

export type DevBoxResource = {
  resource_id: string;
  resource_kind: string;
  provider_resource_id: string | null;
  machine_generation: number;
  retention_state: string;
  deletion_state: string | null;
  encrypted: boolean;
  size_gb: number | null;
};

export type DevBoxAction = 'start' | 'stop' | 'replace' | 'release';
export type WorkspaceRetention = 'retain' | 'delete';

export type LifecycleStep = 'requested' | 'launching' | 'running' | 'installing_crew' | 'enrolling_host' | 'starting_crew' | 'crew_ready';

/**
 * What GET /crew/target last said about this box:
 *   unknown              not asked yet
 *   ready                200 and the lease names exactly this desktop + generation
 *   crew_target_missing  404 — Crew is installed but this host has not been enrolled from the owner's browser
 *   lease_fenced         409 — the host lease exists but is fenced or not yet verified by the controller
 */
export type CrewTargetState = 'unknown' | 'ready' | 'crew_target_missing' | 'lease_fenced';
export type LifecycleEvent = { step: LifecycleStep; at: string; detail?: string };

/** Machine-readable failure plus the provider's own naming, for pages that show more than the code. */
export type DevBoxFailure = {
  code: string;
  /** `error_class` from the failed operation receipt, when the controller named one. */
  errorClass?: string | null;
  /** `progress` of the operation (or box status) when it failed — the step that broke. */
  progress?: string | null;
  /** Server `detail` text for HTTP refusals (403/409), already safe to show. */
  detail?: string | null;
};

type CrewTarget = { health: string; lease: { target_kind: string; target_ref: string; generation: number } };

/** Operation receipts (launch/start/stop): up to 5 minutes. */
const OPERATION_ATTEMPTS = 300;
/** Running → Crew ready (user-data bootstrap, host attestation, enrollment, Crew start): up to 10 minutes. */
const READY_ATTEMPTS = 600;
const POLL_INTERVAL_MS = 1000;
const FAILURE_CODE = /^[a-z_]+$/;

export function costSummary(cost: DevBoxCost | null) {
  if (!cost) return 'Infrastructure cost: not yet observed.';
  const retained = cost.retained_resources?.filter(resource => resource.kind === 'workspace_disk' || resource.kind === 'snapshot') ?? [];
  const storage = retained.length
    ? ` Retained storage: ${retained.map(resource => `${resource.kind} (${resource.size_gb ?? 'unknown'} GB)`).join(', ')}; storage may still incur charges.`
    : '';
  const snapshot = cost.by_kind?.snapshot;
  const snapshotCost = snapshot && retained.some(resource => resource.kind === 'snapshot')
    ? ` Snapshot storage estimate: ${snapshot.storage_gb ?? 'unknown'} GB; ${snapshot.rate_usd_per_gb_hour === null ? 'rate unavailable' : `$${snapshot.rate_usd_per_gb_hour}/GB-hour`}; reported snapshot cost $${snapshot.known_cost_usd}.`
    : '';
  const estimate = `Estimated usage: $${cost.estimated_cost_usd}.`;
  const reconciled = cost.reconciled_cost_usd === null ? '' : ` Reconciled usage: $${cost.reconciled_cost_usd}.`;
  const total = cost.total_cost_usd === null ? ' Total cost unknown.' : ` Reported total: $${cost.total_cost_usd}.`;
  return `${estimate}${reconciled}${total} Coverage: ${cost.coverage}.${storage}${snapshotCost}`;
}

/** Human sentence for a failure; names the controller's error class and the step, never raw JSON. */
export function describeFailure(failure: DevBoxFailure | null): string | null {
  if (!failure) return null;
  if (failure.code === 'crew_migration_blocked') {
    return 'Crew upgrade blocked: workspace compatibility is not verified; existing data is preserved.';
  }
  if (failure.code === 'crew_target_missing') return CREW_TARGET_SENTENCE.crew_target_missing;
  if (failure.code === 'lease_fenced') return CREW_TARGET_SENTENCE.lease_fenced;
  if (failure.code === 'dev_box_not_ready') return 'Your Dev Box is not running with Crew ready. Open it from the Dev Box page.';
  if (failure.code === 'enrollment_refused') return `Host enrollment refused: ${failure.detail ?? 'Enrollment unavailable. Check your owned Crew box and try again.'}`;
  if (failure.detail) return failure.detail;
  if (failure.errorClass === 'provider_refused:InsufficientInstanceCapacity' || failure.errorClass === 'provider_refused:Unsupported') {
    // #8423: the controller already tried every approved size and subnet; a Retry admits a new launch.
    return `EC2 had no capacity for any approved instance size in the approved subnets (${failure.errorClass}). Retry launches again; capacity usually returns within minutes.`;
  }
  if (failure.errorClass === 'workspace_zone_unavailable') {
    return 'No approved subnet lies in the availability zone of your retained workspace disk; an administrator must approve one before the box can be replaced.';
  }
  // Names stay verbatim: they are the identifiers an operator will search the controller logs for.
  const named = failure.errorClass && failure.errorClass !== failure.code ? `${failure.code} — ${failure.errorClass}` : failure.code;
  return failure.progress ? `${named} at step “${failure.progress}”` : named;
}

/**
 * The settled state a box is in, derived from status + observed_state (+ desired_state when
 * present). Only `running` and `stopped` may offer state-changing actions; anything else is a
 * transition the controller owns (requested launch, replace in flight, start/stop not yet
 * observed) or a terminal state, and the page shows the lifecycle only.
 */
export type BoxState = 'running' | 'stopped' | 'transitioning' | 'failed' | 'released';

export function boxState(box: Pick<DevBox, 'status' | 'observed_state' | 'desired_state'> | null | undefined): BoxState | null {
  if (!box) return null;
  if (box.status === 'released') return 'released';
  if (box.status === 'failed') return 'failed';
  const settled = (state: 'running' | 'stopped') =>
    box.status === state && box.observed_state === state && (box.desired_state === undefined || box.desired_state === state);
  if (settled('running')) return 'running';
  if (settled('stopped')) return 'stopped';
  return 'transitioning';
}

/** One sentence per named Crew-target state, shared by the page and the chat action. */
export const CREW_TARGET_SENTENCE: Record<Exclude<CrewTargetState, 'unknown'>, string> = {
  ready: 'Crew is ready on this host.',
  crew_target_missing: 'Crew is installed; opening the box enrolls this host automatically.',
  lease_fenced: 'Host enrolled; the controller is binding this host and verifying Crew on it.',
};

/** Classify a thrown apiClient error from GET /crew/target; null when it is not one of the named states. */
/** Why a running host is still fenced, when the controller has named the host's refusal; else null. */
/**
 * Only a generation nobody's browser has approved needs this browser's enrollment. With the binding
 * state from the API, `none` is the one answer that means "enroll"; a fenced or missing target on an
 * `enrolled`/`active`/`pending_enrollment` host is the controller's to resolve. Older gateways without
 * the field keep the pre-#8223 rule: enroll on a missing target only.
 */
export function needsBrowserEnrollment(box: Pick<DevBox, 'host_binding'>, target: CrewTargetState): boolean {
  if (typeof box.host_binding === 'string') return box.host_binding === 'none' && target !== 'ready';
  return target === 'crew_target_missing';
}

export function hostReadinessSentence(box: Pick<DevBox, 'host_readiness_refusal'> | null | undefined): string | null {
  const refusal = box?.host_readiness_refusal;
  if (!refusal) return null;
  const reason = refusal.startsWith('host_refused:') ? refusal.slice('host_refused:'.length) : refusal;
  if (reason === 'CrewProfileMismatch') {
    return 'This machine runs a different Crew release than its record says. Replace the box to update it; the controller keeps retrying meanwhile.';
  }
  if (reason === 'CrewReceiptMissing' || reason === 'CrewReceiptRefused' || reason === 'CrewReceiptUntrusted') {
    return `Crew was never installed on this machine (${reason}); Replace the box.`;
  }
  return `Crew could not start on this host (${reason}); the controller retries automatically. If it persists, Replace the box.`;
}

export function crewTargetError(error: unknown): Exclude<CrewTargetState, 'unknown' | 'ready'> | null {
  const status = httpStatus(error);
  if (status === 404) return 'crew_target_missing';
  if (status === 409) return 'lease_fenced';
  return null;
}

class DevBoxError extends Error {
  failure: DevBoxFailure;
  constructor(failure: DevBoxFailure) {
    super(failure.code);
    this.failure = failure;
  }
}

function asFailure(error: unknown): DevBoxFailure {
  if (error instanceof DevBoxError) return error.failure;
  if (error instanceof Error && FAILURE_CODE.test(error.message)) return { code: error.message };
  if (error && typeof error === 'object' && 'status' in error) {
    const detail = 'detail' in error && typeof error.detail === 'string' ? error.detail : null;
    return { code: 'desktop_request_refused', detail };
  }
  return { code: 'EC2 Crew unavailable' };
}

function httpStatus(error: unknown): number | null {
  return error && typeof error === 'object' && 'status' in error && typeof error.status === 'number' ? error.status : null;
}

/** The server's `detail` as text: a plain string, or the `detail`/`reason`/`error` of a structured detail. */
function detailText(error: unknown): string | null {
  if (!error || typeof error !== 'object' || !('detail' in error)) return null;
  const detail = error.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    for (const key of ['detail', 'reason', 'error'] as const) {
      const value = (detail as Record<string, unknown>)[key];
      if (typeof value === 'string' && value) return value;
    }
  }
  return null;
}

function detailError(error: unknown): string | null {
  const detail = error && typeof error === 'object' && 'detail' in error ? error.detail : null;
  return detail && typeof detail === 'object' && typeof (detail as { error?: unknown }).error === 'string' ? (detail as { error: string }).error : null;
}

type EnrollmentOutcome = 'submitted' | 'pending' | 'host_not_verified';

/**
 * Run the headless enrollment once and classify the API's answer:
 *   submitted          sealed challenge accepted — the controller will verify and start Crew
 *   pending            409 "A host enrollment is pending" / "A host run is already active" — await, do not resubmit
 *   host_not_verified  409 the controller has not attested this host (or its key) yet — try again on the next poll
 * Anything else is a refusal, surfaced by name with the server's own sentence.
 */
async function enrollHost(desktopId: string, signal: AbortSignal): Promise<EnrollmentOutcome> {
  try {
    await enrollCrewHost(desktopId, signal);
    return 'submitted';
  } catch (error) {
    const status = httpStatus(error);
    const detail = detailText(error);
    if (status === 409 && detail && /enrollment is pending|run is already active/i.test(detail)) return 'pending';
    if (status === 409 && detail && /has not been verified|is not available|not current|generation changed/i.test(detail)) return 'host_not_verified';
    throw new DevBoxError({ code: 'enrollment_refused', detail: status !== null ? detail : null });
  }
}

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));

export type OpenBoxOptions = {
  /** Receives the validated handoff URL; defaults to a same-tab `location.replace`. */
  deliver?: (destination: string) => void;
  /** `false`: connect only — never provision, start or enroll (the interstitial without a click intent). */
  ensure?: boolean;
};

const replaceLocation = (destination: string) => { window.location.replace(destination); };

function desktopPath(desktopId: string) {
  return `/cloud-desktops/${encodeURIComponent(desktopId)}`;
}

export function useDevBox({ autoLoad = false }: { autoLoad?: boolean } = {}) {
  const [box, setBox] = useState<DevBox | null>(null);
  const [cost, setCost] = useState<DevBoxCost | null>(null);
  const [policy, setPolicy] = useState<DevBoxPolicy | null>(null);
  const [resources, setResources] = useState<DevBoxResource[] | null>(null);
  const [phase, setPhase] = useState<string | null>(null);
  const [failure, setFailure] = useState<DevBoxFailure | null>(null);
  const [timeline, setTimeline] = useState<LifecycleEvent[]>([]);
  const [crewTarget, setCrewTarget] = useState<CrewTargetState>('unknown');
  const [loading, setLoading] = useState(autoLoad);
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const crewStarting = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; request.current?.abort(); };
  }, []);

  const mark = useCallback((step: LifecycleStep, detail?: string) => {
    setTimeline(previous => previous.some(event => event.step === step) ? previous
      : [...previous, { step, at: new Date().toISOString(), detail }]);
  }, []);

  const adoptBox = useCallback((latest: DevBox) => { setBox(latest); setCost(latest.infrastructure_cost); }, []);

  /** Owned non-released boxes; the ensure contract allows at most one. */
  const listCurrent = useCallback(async (signal: AbortSignal): Promise<DevBox[]> => {
    const boxes = await apiClient.get<{ contract_version: number; items: DevBox[] }>('/cloud-desktops', signal);
    if (boxes.contract_version !== 1 || !Array.isArray(boxes.items)) throw new DevBoxError({ code: 'desktop_status_invalid' });
    return boxes.items.filter(item => item.status !== 'released');
  }, []);

  const pollOperation = useCallback(async (desktopId: string, operationId: string, signal: AbortSignal) => {
    for (let attempt = 0; attempt < OPERATION_ATTEMPTS && !signal.aborted; attempt++) {
      const operation = await apiClient.get<DevBoxOperation>(
        `${desktopPath(desktopId)}/operations/${encodeURIComponent(operationId)}`, signal,
      );
      if (operation.desktop_id !== desktopId || operation.operation_id !== operationId) throw new DevBoxError({ code: 'desktop_status_invalid' });
      setPhase(`EC2 operation: ${operation.progress}`);
      // The receipt exists, so the provider has been asked: that is "launching" even when the
      // first poll already reports the outcome.
      mark('launching', operation.progress);
      if (operation.status === 'failed') {
        throw new DevBoxError({
          code: operation.error_class === 'crew_migration_blocked' ? 'crew_migration_blocked' : 'desktop_operation_failed',
          errorClass: operation.error_class ?? null,
          progress: operation.progress,
        });
      }
      if (operation.status === 'succeeded') return;
      if (operation.status !== 'pending' && operation.status !== 'unknown') throw new DevBoxError({ code: 'desktop_status_invalid' });
      await sleep(POLL_INTERVAL_MS);
    }
    if (!signal.aborted) throw new DevBoxError({ code: 'desktop_operation_timeout' });
  }, [mark]);

  /**
   * Ask GET /crew/target once and record the named state. 200 must name exactly this box and
   * generation; anything else is a mismatch, never "ready".
   */
  const checkTarget = useCallback(async (current: DevBox, signal?: AbortSignal): Promise<CrewTargetState> => {
    let target: CrewTarget | null = null;
    try {
      target = await apiClient.get<CrewTarget>('/crew/target', signal);
    } catch (error) {
      if (detailError(error) === 'crew_scope_refused') {
        throw new DevBoxError({ code: 'crew_scope_refused', detail: detailText(error) });
      }
      const named = crewTargetError(error);
      if (!named) throw error;
      // #9619: a 409 crew_starting is the controller saying "machine up, Crew not answering yet";
      // it is waited on like a fenced lease but named as starting while it lasts.
      crewStarting.current = detailError(error) === 'crew_starting';
      if (mounted.current) setCrewTarget(named);
      return named;
    }
    if (target.health !== 'ready' || target.lease?.target_kind !== 'ec2-instance' ||
        target.lease.target_ref !== current.desktop_id || target.lease.generation !== current.machine_generation) {
      if (mounted.current) setCrewTarget('unknown');
      throw new DevBoxError({ code: 'crew_target_mismatch' });
    }
    if (mounted.current) setCrewTarget('ready');
    return 'ready';
  }, []);

  /**
   * Ensure the canonical Crew box exists and is running, then wait until the controller
   * has verified Crew readiness for exactly this host generation. Resolves with the last
   * verified desktop read. Throws DevBoxError with a stable code (and the provider's
   * error_class/progress when an operation failed).
   */
  const ensureReady = useCallback(async (signal: AbortSignal): Promise<DevBox> => {
    setPhase('Checking your owned EC2 box…');
    const current = await listCurrent(signal);
    if (current.length > 1 || current.some(item => item.profile !== 'crew')) throw new DevBoxError({ code: 'desktop_profile_conflict' });
    const template = current.length ? current[0].template :
      (await apiClient.get<{ contract_version: number; template: string }>('/cloud-desktops/crew-template', signal)).template;
    if (typeof template !== 'string' || !template) throw new DevBoxError({ code: 'desktop_template_unavailable' });
    if (current.length) adoptBox(current[0]);
    const admitted = await apiClient.request<DevBoxOperation>('/cloud-desktops/ensure', {
      method: 'POST', body: { template, profile: 'crew' }, signal,
      headers: { 'Idempotency-Key': crypto.randomUUID() },
    });
    if (!admitted.desktop_id || (current.length && admitted.desktop_id !== current[0].desktop_id)) throw new DevBoxError({ code: 'desktop_status_invalid' });
    mark('requested', admitted.progress);
    if (admitted.operation_id !== null) {
      if (typeof admitted.operation_id !== 'string' || !admitted.operation_id) throw new DevBoxError({ code: 'desktop_status_invalid' });
      await pollOperation(admitted.desktop_id, admitted.operation_id, signal);
    }
    const enrollment = { submitted: false };
    for (let attempt = 0; attempt < READY_ATTEMPTS && !signal.aborted; attempt++) {
      const latest = await apiClient.get<DevBox>(desktopPath(admitted.desktop_id), signal);
      if (latest.desktop_id !== admitted.desktop_id || latest.profile !== 'crew' || !Number.isSafeInteger(latest.machine_generation) || latest.machine_generation < 1 ||
          (current.length && latest.machine_generation < current[0].machine_generation)) throw new DevBoxError({ code: 'desktop_status_invalid' });
      adoptBox(latest);
      if (latest.status === 'released' || latest.status === 'failed') {
        throw new DevBoxError({ code: 'desktop_operation_failed', progress: latest.status });
      }
      if (latest.status === 'running' && latest.observed_state === 'running') {
        mark('running');
        mark('installing_crew', latest.crew_bootstrap === 'user-data' ? 'user-data bootstrap' : undefined);
        setPhase('Machine running; checking Crew on this host…');
        const target = await checkTarget(latest, signal);
        if (target === 'ready') {
          const verified = await apiClient.get<DevBox>(desktopPath(latest.desktop_id), signal);
          if (verified.desktop_id !== latest.desktop_id || verified.machine_generation !== latest.machine_generation ||
              verified.status !== 'running' || verified.observed_state !== 'running')
            throw new DevBoxError({ code: 'desktop_status_invalid' });
          adoptBox(verified);
          mark('enrolling_host');
          mark('starting_crew');
          mark('crew_ready');
          return verified;
        }
        if (needsBrowserEnrollment(latest, target) && !enrollment.submitted) {
          // This generation was never approved from a browser: enroll it from this one, once. The device
          // key never leaves here. A stopped-and-started box is NOT enrolled again: its approved device
          // survives the stop and the controller re-binds the host run on start by itself, so a fenced
          // lease there is a wait, never a second enrollment (#8223 simplification). A truly pending
          // enrollment or active run is refused by the API itself and awaited through `enrollHost`'s 'pending'.
          mark('enrolling_host');
          setPhase('Enrolling this host from your browser…');
          const outcome = await enrollHost(latest.desktop_id, signal);
          if (outcome === 'host_not_verified') {
            setPhase('Waiting for the controller to verify the host before enrolling…');
          } else {
            enrollment.submitted = true;
            mark('starting_crew', outcome === 'pending' ? 'enrollment already pending' : undefined);
            setPhase('Host enrolled; waiting for Crew to start…');
          }
        } else {
          // Enrolled (this time or on an earlier start): the controller is binding the run and
          // verifying Crew on the host; nothing for the browser to do but wait and name the reason.
          mark('enrolling_host');
          mark('starting_crew');
          setPhase(target === 'lease_fenced'
            ? (crewStarting.current
              ? 'Machine running; Kiro Crew is starting on the box…'
              : `Machine running; ${hostReadinessSentence(latest) ?? CREW_TARGET_SENTENCE.lease_fenced}`)
            : 'Host enrolled; waiting for Crew to start…');
        }
      } else setPhase(`Machine ${latest.status}; Crew readiness not verified.`);
      await sleep(POLL_INTERVAL_MS);
    }
    throw new DevBoxError({ code: 'crew_readiness_timeout' });
  }, [adoptBox, checkTarget, listCurrent, mark, pollOperation]);

  /** Runs `work` as the single in-flight request; reports failures through `failure`. */
  const run = useCallback(async <T>(work: (signal: AbortSignal) => Promise<T>, onFailure?: () => void): Promise<T | null> => {
    if (request.current) return null;
    const controller = new AbortController();
    request.current = controller;
    setFailure(null);
    try {
      return await work(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted && mounted.current) {
        onFailure?.();
        setFailure(asFailure(error));
        setPhase(null);
      }
      return null;
    } finally {
      if (request.current === controller) request.current = null;
    }
  }, []);

  /**
   * Load the owned box (plus resources) or, with no box, the admission policy. `quiet` is for the
   * re-read after an action: a failure there must not overwrite the action's own named failure.
   */
  const refresh = useCallback(async ({ quiet = false }: { quiet?: boolean } = {}) => {
    const controller = new AbortController();
    try {
      const current = await listCurrent(controller.signal);
      if (current.length === 1 && current[0].profile === 'crew') {
        adoptBox(current[0]);
        setPolicy(null);
        if (current[0].status === 'running' && current[0].observed_state === 'running') {
          try { await checkTarget(current[0], controller.signal); } catch { /* mismatch is reported by the next openBox() */ }
        } else {
          setCrewTarget('unknown');
        }
        try {
          const found = await apiClient.get<{ contract_version: number; items: DevBoxResource[] }>(`${desktopPath(current[0].desktop_id)}/resources`, controller.signal);
          if (mounted.current) setResources(Array.isArray(found.items) ? found.items : []);
        } catch {
          if (mounted.current) setResources(null);
        }
      } else if (current.length === 0) {
        setBox(null);
        setResources(null);
        const admission = await apiClient.get<DevBoxPolicy>('/cloud-desktops/policy', controller.signal);
        if (mounted.current) setPolicy(admission);
      } else {
        throw new DevBoxError({ code: 'desktop_profile_conflict' });
      }
    } catch (error) {
      if (mounted.current && !quiet) setFailure(asFailure(error));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [adoptBox, checkTarget, listCurrent]);

  useEffect(() => { if (autoLoad) void refresh(); }, [autoLoad, refresh]);

  /**
   * Connect-only check for a tab that was not opened by the primary action (the interstitial
   * visited directly, reloaded, or reached from a stale link): never provisions or starts
   * anything. Resolves only when the one owned box is running and GET /crew/target names it.
   */
  const requireReady = useCallback(async (signal: AbortSignal): Promise<DevBox> => {
    setPhase('Checking your Dev Box…');
    const current = await listCurrent(signal);
    if (current.length !== 1 || current[0].profile !== 'crew') throw new DevBoxError({ code: 'dev_box_not_ready' });
    adoptBox(current[0]);
    if (current[0].status !== 'running' || current[0].observed_state !== 'running') throw new DevBoxError({ code: 'dev_box_not_ready' });
    if (await checkTarget(current[0], signal) !== 'ready') throw new DevBoxError({ code: 'dev_box_not_ready' });
    return current[0];
  }, [adoptBox, checkTarget, listCurrent]);

  /**
   * The one owner action (#8140): ensure/start the box → wait until it runs → enroll this host
   * automatically when the Crew target is missing (a pending enrollment is awaited, not
   * re-submitted) → wait for GET /crew/target 200 → resolve the handoff destination → deliver
   * it. This runs in the tab that will BECOME the dashboard (`pages/settings/DevBoxOpen`), so
   * the default delivery is a same-tab `location.replace`: the handoff is a top-level
   * navigation, which is what lets the Crew origin set its first-party cookie, and the
   * interstitial never stays in history. The page that owns the click opens that tab
   * synchronously (see `services/devBoxOpen`), so no late pop-up exists to be blocked.
   * `ensure: false` is the connect-only variant (`requireReady`).
   * Idempotent: a call while a run is in flight does nothing (`run` keeps one request).
   */
  const openBox = useCallback(async (handoffUrl: string, { deliver = replaceLocation, ensure = true }: OpenBoxOptions = {}) => {
    if (request.current) return null;
    setCost(null);
    setTimeline([]);
    const opened = await run(async signal => {
      if (ensure) await ensureReady(signal); else await requireReady(signal);
      const destination = await resolveCrewDashboardDestination(handoffUrl);
      setPhase(null);
      deliver(destination);
      return destination;
    });
    if (ensure) void refresh({ quiet: true });
    return opened;
  }, [ensureReady, refresh, requireReady, run]);

  /** start | stop | replace | release with a fresh idempotency key; polls the receipt, then refreshes. */
  const act = useCallback(async (action: DevBoxAction, retention?: WorkspaceRetention, snapshotId?: string) => {
    const target = box;
    if (!target) return null;
    if (action === 'release' && !retention) {
      setFailure({ code: 'workspace_retention_required' });
      return null;
    }
    return run(async signal => {
      setPhase(`Requesting ${action}…`);
      const receipt = await apiClient.request<DevBoxOperation>(`${desktopPath(target.desktop_id)}/${action}`, {
        method: 'POST', signal,
        body: action === 'release' ? { workspace_retention: retention } : action === 'replace' && snapshotId ? { snapshot_id: snapshotId } : undefined,
        headers: { 'Idempotency-Key': crypto.randomUUID() },
      });
      if (receipt.desktop_id !== target.desktop_id || typeof receipt.operation_id !== 'string' || !receipt.operation_id) {
        throw new DevBoxError({ code: 'desktop_status_invalid' });
      }
      await pollOperation(target.desktop_id, receipt.operation_id, signal);
      setPhase(null);
      await refresh({ quiet: true });
      return receipt;
    });
  }, [box, pollOperation, refresh, run]);

  const cancel = useCallback(() => { request.current?.abort(); request.current = null; setPhase(null); }, []);

  return {
    box, cost, policy, resources, phase, failure, timeline, loading, crewTarget,
    busy: phase !== null,
    refresh, openBox, act, cancel,
  };
}
