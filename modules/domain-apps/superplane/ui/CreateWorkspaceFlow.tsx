/**
 * Review-then-submit workspace creation — #5730 AC-02.
 *
 * WHY THERE IS A REVIEW STEP AT ALL
 * ---------------------------------
 * Creating a workspace provisions infrastructure and spends money. AC-02 requires
 * submission to reach "durable progress and verified readiness", and the design
 * requires it to be bound to "the exact reviewed plan revision". Both point at the
 * same shape: the server validates the inputs and returns a plan with a revision,
 * the user confirms *that* plan, and the submission carries that revision. If the
 * inputs change after the plan was fetched, the confirmation the user gave no
 * longer describes what would be built, so the plan is discarded and must be
 * fetched again rather than silently reused.
 *
 * WHY THE OPERATION IDENTITY IS MINTED BEFORE THE FIRST ATTEMPT
 * ------------------------------------------------------------
 * Because the identity has to survive the events that create duplicates, and
 * those events happen *during* the request: a double click, a reload while the
 * spinner is up, a proxy timing out after the server already accepted. An identity
 * minted from the response cannot deduplicate the request that produced it. So it
 * is minted and persisted first, then reused by every subsequent attempt at the
 * same intent — see operations.ts.
 *
 * WHAT THIS DELIBERATELY WILL NOT DO
 * ----------------------------------
 * It will not submit at all against a server that has not advertised
 * `CREATE_IDEMPOTENCY_FEATURE`. `POST /workspaces` currently validates its body
 * with a Pydantic model that has no operation-identity field, and Pydantic ignores
 * unknown fields by default — so the create would return 201 while deduplicating
 * nothing, and the user would be told their submission was idempotent when it was
 * not. A create that is refused is recoverable. A create that silently spends
 * twice is not. This is why the whole flow is gated rather than optimistic.
 *
 * Because the plan and capability endpoints are both unserved on the current
 * baseline (#5535), what this renders today is the honest blocked state. The logic
 * is written against the contract so that flipping `served` activates it, and the
 * tests drive both worlds.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { Alert, Button, Input, Select, Spinner } from '@/components/ui';

import { ApprovalPanel } from './ApprovalPanel';
import { LifecycleProposalPanel } from './LifecycleProposalPanel';
import { useFreshnessClock } from './readiness';

import {
  ScopeGuard,
  adoptWorkspace,
  getOperation,
  recoverOperation,
  requestApproval,
  getApproval,
  createWorkspace,
  isSuperseded,
  previewWorkspace,
  type Outcome,
} from './client';
import {
  CREATE_IDEMPOTENCY_FEATURE,
  ENDPOINTS,
  OPERATION_ID_FIELD,
  inputRefusals,
  offeredIsolationModes,
  type Capabilities,
  type IsolationMode,
  type OnboardingInputs,
  type OnboardingPlan,
  type OperationApproval,
  type Unavailable,
} from './contract';
import {
  claimPreviewIdentity,
  markSubmissionStage,
  fingerprint,
  isTerminal,
  stateFromWorkspaceStatus,
  readReceipt,
  recordObservationExclusive,
  type ExclusiveSection,
  type ReceiptScope,
  type ReceiptStore,
  type StoredReceipt,
} from './operations';

/** The intent name under which this flow's receipt is stored. */
export const CREATE_INTENT = 'create-workspace';

/**
 * The submission payload, built from inputs and the reviewed plan.
 *
 * Exported and pure so the CLI can build a byte-identical body from the same
 * inputs (AC-07 parity). If the two clients constructed payloads separately they
 * would fingerprint differently, and the same intent submitted from the UI and
 * then retried from the CLI would look like two intents — defeating the identity
 * guarantee precisely when a user reaches for the CLI because the UI failed.
 */
export function buildCreatePayload(
  inputs: OnboardingInputs,
  plan: OnboardingPlan,
  operationId: string,
): Record<string, unknown> {
  return {
    mode: inputs.mode,
    cluster_reference: inputs.clusterReference ?? null,
    name: inputs.name,
    isolation_mode: inputs.isolationMode,
    account: inputs.account ?? null,
    region: inputs.region ?? null,
    budget_max_daily_usd: inputs.budgetMaxDailyUsd ?? null,
    budget_max_gpus: inputs.budgetMaxGpus ?? null,
    // The revision binds this submission to the plan the user actually saw. A
    // server that has moved on rejects it rather than building something else.
    plan_revision: plan.revision,
    [OPERATION_ID_FIELD]: operationId,
  };
}

type Stage =
  | { name: 'editing' }
  | { name: 'planning' }
  | { name: 'reviewing'; plan: OnboardingPlan; planFor: string }
  | { name: 'submitting'; plan: OnboardingPlan }
  | { name: 'submitted'; receipt: StoredReceipt }
  | { name: 'blocked'; unavailable: Unavailable }
  | { name: 'conflict'; detail: string };

export interface CreateWorkspaceFlowProps {
  guard: ScopeGuard;
  scope: ReceiptScope;
  store: ReceiptStore;
  /** Set when this environment cannot honour a submitted operation identity. */
  idempotencySupport: Unavailable | null;
  /**
   * The deployment's capability report, or null when it could not be read.
   *
   * Used for the isolation modes it says it serves. Null is not a reason to offer
   * nothing: `offeredIsolationModes` falls back to the set the server's own schema
   * validates against, every member of which it accepts by definition.
   */
  capabilities?: Capabilities | null;
  /**
   * Re-checked immediately before submitting, after the user has confirmed.
   *
   * Separate from `idempotencySupport`, which governs the affordance, because the
   * two answer different questions at different times. The affordance is decided
   * at render; this is decided at the last moment before an irreversible, paid
   * action, and capability discovery can resolve or change in between — a user can
   * sit on a reviewed plan for minutes. Checking only at render means a create can
   * be submitted under a guarantee that has since stopped holding.
   *
   * Defaults to the render-time answer, so the common case needs no wiring.
   */
  verifyBeforeSubmit?: () => Unavailable | null;
  /** Injected so tests assert identity reuse without randomness. */
  mintKey?: () => string;
  nowIso?: () => string;
  /**
   * The exclusive section the identity claim runs in.
   *
   * Injected so a test can interleave two claims deterministically at the exact
   * point the race happens, rather than starting two and hoping the scheduler
   * produces the damaging order.
   */
  section?: ExclusiveSection;
  onCreated?: () => void;
  onCancel?: () => void;
}

export function CreateWorkspaceFlow({
  guard,
  scope,
  store,
  idempotencySupport,
  capabilities = null,
  verifyBeforeSubmit,
  mintKey = () => crypto.randomUUID(),
  nowIso = () => new Date().toISOString(),
  section,
  onCreated,
  onCancel,
}: CreateWorkspaceFlowProps) {
  const now = useFreshnessClock();
  const modes = useMemo(() => offeredIsolationModes(capabilities), [capabilities]);

  const [inputs, setInputs] = useState<OnboardingInputs>({
    mode: 'managed',
    name: '',
    // `dedicated` is the schema's own default
    // (`CreateWorkspaceRequest.isolation_mode`), so an untouched form submits what
    // the server would have chosen anyway. The previous default, `'shared'`, is not
    // in the accepted pattern at all: every submission from an untouched form was a
    // 422 waiting to happen, and the user could not change it.
    isolationMode: 'dedicated',
  });
  const [stage, setStage] = useState<Stage>({ name: 'editing' });

  /**
   * A receipt already on disk means an earlier attempt at this same intent exists
   * — from a previous session, a reload mid-submission, or a lost reply. It is
   * surfaced rather than ignored, because the alternative is a user who resubmits
   * to "try again" and builds a second workspace.
   */
  const [existing, setExisting] = useState(() => readReceipt(store, scope, CREATE_INTENT));
  const [approval, setApproval] = useState<OperationApproval | null>(null);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [recoveryProblem, setRecoveryProblem] = useState<Unavailable | null>(null);
  const currentReceipt = stage.name === 'submitted' ? stage.receipt : existing;
  const refreshReceipt = useCallback(async () => {
    const receipt = readReceipt(store, scope, CREATE_INTENT);
    if (!receipt) return;
    const observed = receipt.operationId
      ? await getOperation(guard, receipt.operationId)
      : await recoverOperation(guard, receipt.idempotencyKey);
    if (isSuperseded(observed)) return;
    if (!observed.ok) {
      if ('unavailable' in observed) setRecoveryProblem(observed.unavailable);
      return;
    }
    if (observed.value.idempotencyKey !== receipt.idempotencyKey) {
      setRecoveryProblem({ reason: 'unknown', detail: 'The response names a different request. The original receipt was retained.' });
      return;
    }
    const updated = await recordObservationExclusive(store, scope, CREATE_INTENT, observed.value, section);
    if (updated) {
      setExisting(updated);
      setStage((previous) => previous.name === 'submitted' ? { name: 'submitted', receipt: updated } : previous);
      setRecoveryProblem(null);
    }
  }, [guard, scope, store, section]);

  useEffect(() => {
    if (!currentReceipt || (currentReceipt.submissionStage && currentReceipt.submissionStage !== 'submitted') || isTerminal(currentReceipt.state)) return;
    void refreshReceipt();
    const timer = setInterval(() => void refreshReceipt(), 5000);
    return () => clearInterval(timer);
  }, [currentReceipt?.idempotencyKey, currentReceipt?.state, currentReceipt?.submissionStage, refreshReceipt]);

  useEffect(() => {
    const approvalId = currentReceipt?.approvalId;
    if (!approvalId) return;
    const refresh = async () => {
      const result = await getApproval(guard, approvalId);
      if (result.ok) setApproval(result.value);
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [currentReceipt?.approvalId, guard]);

  /** The current inputs' fingerprint, used to invalidate a stale plan. */
  const inputsPrint = fingerprint(inputs);

  /**
   * Why these inputs cannot be submitted, per the server's own validators.
   *
   * Computed from the same rules `CreateWorkspaceRequest` enforces, so a research
   * workspace with no account is refused here with the reason instead of being
   * sent and coming back a 422 the user has to decode.
   */
  const refusals = inputRefusals(inputs);

  const update = useCallback(
    (patch: Partial<OnboardingInputs>) => {
      setInputs((previous) => ({ ...previous, ...patch }));
      // Any input change invalidates a reviewed plan: the confirmation the user is
      // about to give must describe what would actually be built.
      setStage((current) =>
        current.name === 'reviewing' || current.name === 'planning'
          ? { name: 'editing' }
          : current,
      );
    },
    [],
  );

  const requestPlan = useCallback(async () => {
    setStage({ name: 'planning' });
    const payload = buildCreatePayload(inputs, { revision: '' } as OnboardingPlan, '');
    delete payload[OPERATION_ID_FIELD];
    delete payload.plan_revision;
    const generation = guard.current();
    const claim = await claimPreviewIdentity(store, scope, CREATE_INTENT, payload, mintKey, nowIso(), section);
    if (!guard.isCurrent(generation)) return;
    if (claim.kind === 'conflict') { setStage({ name: 'conflict', detail: claim.detail }); return; }
    if (existing?.idempotencyKey !== claim.receipt.idempotencyKey) setApproval(null);
    setExisting(claim.receipt);
    payload[OPERATION_ID_FIELD] = claim.receipt.idempotencyKey;
    const outcome: Outcome<OnboardingPlan> = await previewWorkspace(guard, payload);
    if (isSuperseded(outcome)) return;
    if (!outcome.ok && 'unavailable' in outcome) {
      // The honest state on the current baseline: the plan route is not served, so
      // there is no reviewable plan and therefore nothing safe to submit.
      setStage({ name: 'blocked', unavailable: outcome.unavailable });
      return;
    }
    if (outcome.ok) {
      setStage({ name: 'reviewing', plan: outcome.value, planFor: inputsPrint });
    }
  }, [guard, inputs, inputsPrint, store, scope, mintKey, nowIso, section, existing?.idempotencyKey]);

  const submit = useCallback(
    async (plan: OnboardingPlan) => {
      if (capabilities && !capabilities.modes.includes(inputs.mode)) {
        setStage({ name: 'blocked', unavailable: { reason: 'not-deployed', detail: 'This workspace mode is unavailable on this deployment.' } });
        return;
      }
      // Re-verified here, not merely at render. This is the last moment before a
      // paid, irreversible action, and the render-time answer may be minutes old.
      const blocker = verifyBeforeSubmit ? verifyBeforeSubmit() : idempotencySupport;
      if (blocker) {
        setStage({ name: 'blocked', unavailable: blocker });
        return;
      }

      const generation = guard.current();
      const receipt = readReceipt(store, scope, CREATE_INTENT);
      if (!receipt) { setStage({ name: 'editing' }); return; }
      if (plan.approvalRequired && (!approval || approval.result !== 'allowed-once' || approval.revoked || !Number.isFinite(Date.parse(approval.expires_at)) || Date.parse(approval.expires_at) <= Date.now())) {
        setRecoveryProblem({ reason: 'not-permitted', detail: 'This exact plan requires a current approval before submission.' });
        return;
      }
      const sealed = await markSubmissionStage(store, scope, CREATE_INTENT, receipt.idempotencyKey, 'submitted', approval?.approval_id, section);
      if (!sealed || !guard.isCurrent(generation)) return;
      const latestBlocker = verifyBeforeSubmit ? verifyBeforeSubmit() : idempotencySupport;
      if (latestBlocker) { setStage({ name: 'blocked', unavailable: latestBlocker }); return; }
      const claim = { receipt: sealed };
      setStage({ name: 'submitting', plan });

      const body = buildCreatePayload(inputs, plan, claim.receipt.idempotencyKey);
      if (approval) body.approval_id = approval.approval_id;
      const outcome = await (inputs.mode === 'adopt' ? adoptWorkspace : createWorkspace)(guard, body);

      if (isSuperseded(outcome)) return;

      if (!outcome.ok && 'unavailable' in outcome) {
        // The reply was not usable. The receipt moves to `unknown`, NOT `failed`:
        // the server may well have accepted this. Recording a failure here is what
        // invites the duplicate.
        const updated = await recordObservationExclusive(store, scope, CREATE_INTENT, {
          idempotencyKey: claim.receipt.idempotencyKey,
          operationId: null,
          state: 'unknown',
          workspaceId: null,
        }, section);
        setStage({
          name: 'submitted',
          receipt: updated ?? { ...claim.receipt, state: 'unknown' },
        });
        return;
      }
      if (!outcome.ok) return;

      // The state the REPLY establishes, which for a create is almost never
      // terminal: the server answers 201 with `status: "Provisioning"` and builds
      // the cluster afterwards. Recording `succeeded` here — as this used to —
      // claims an outcome the server never gave, and a terminal receipt then lets
      // changed inputs mint a second identity while the first is still building.
      const state = outcome.value.operation_state ?? stateFromWorkspaceStatus(outcome.value.status);
      const updated = await recordObservationExclusive(store, scope, CREATE_INTENT, {
        idempotencyKey: claim.receipt.idempotencyKey,
        operationId: outcome.value.provisioning_operation_id ?? null,
        state,
        workspaceId: outcome.value.id,
      }, section);
      setStage({
        name: 'submitted',
        receipt: updated ?? { ...claim.receipt, state },
      });
      onCreated?.();
    },
    [
      guard,
      capabilities,
      approval,
      idempotencySupport,
      inputs,
      mintKey,
      nowIso,
      onCreated,
      scope,
      section,
      store,
      verifyBeforeSubmit,
    ],
  );

  const askApproval = async (plan: OnboardingPlan) => {
    const receipt = readReceipt(store, scope, CREATE_INTENT);
    if (!receipt || !plan.approvalRequest) {
      setRecoveryProblem({ reason: 'not-deployed', detail: 'This deployment did not return an approval request for the reviewed plan.' });
      return;
    }
    const generation = guard.current();
    const sealed = await markSubmissionStage(store, scope, CREATE_INTENT, receipt.idempotencyKey, 'approval', undefined, section);
    if (!sealed || !guard.isCurrent(generation)) return;
    setExisting(sealed);
    setApprovalBusy(true);
    const result = await requestApproval(guard, plan.approvalRequest);
    if (isSuperseded(result)) return;
    setApprovalBusy(false);
    if (!result.ok) {
      if ('unavailable' in result) setRecoveryProblem(result.unavailable);
      return;
    }
    const saved = await markSubmissionStage(store, scope, CREATE_INTENT, receipt.idempotencyKey, 'approval', result.value.approval_id, section);
    if (!guard.isCurrent(generation)) return;
    if (saved) setExisting(saved);
    setApproval(result.value);
    setRecoveryProblem(null);
  };

  const planIsStale = stage.name === 'reviewing' && stage.planFor !== inputsPrint;

  useEffect(() => {
    document.getElementById('superplane-create-heading')?.focus();
  }, []);

  useEffect(() => {
    if (stage.name === 'blocked' || stage.name === 'conflict') {
      document.getElementById('superplane-create-problem')?.focus();
    } else if (stage.name === 'reviewing' && !planIsStale) {
      document.getElementById('superplane-plan-heading')?.focus();
    }
  }, [stage.name, planIsStale]);

  return (
    <section
      aria-labelledby="superplane-create-heading"
      className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800"
    >
      <h2
        id="superplane-create-heading"
        tabIndex={-1}
        className="text-lg font-semibold text-gray-900 dark:text-white"
      >
        Create a workspace
      </h2>

      {existing && existing.state === 'unknown' && (
        <div className="mt-4">
          {/* The lost-reply case. The user must not be told this failed, because
              it may have succeeded, and "try again" would then build a second. */}
          <Alert variant="warning" title="An earlier submission's outcome is unknown">
            A workspace creation was submitted from this browser and its reply was
            never seen, so it is not known whether it completed. Its operation
            identity is preserved, so retrying the same inputs cannot create a
            second workspace. Check the workspace list before changing anything.
          </Alert>
        </div>
      )}

      <div className="mt-4 space-y-4">
        {capabilities?.modes.includes('adopt') && (
          <Select label="Workspace source" name="workspace-mode" value={inputs.mode}
            options={capabilities.modes.map((mode) => ({ value: mode, label: mode === 'adopt' ? 'Adopt existing cluster' : 'Create managed cluster' }))}
            onChange={(event) => update({ mode: event.target.value as OnboardingInputs['mode'] })} />
        )}
        {inputs.mode === 'adopt' && (
          <Input label="Cluster reference" name="workspace-cluster" value={inputs.clusterReference ?? ''}
            onChange={(event) => update({ clusterReference: event.target.value })} required />
        )}
        <Input
          name="workspace-name"
          label="Workspace name"
          value={inputs.name}
          onChange={(event) => update({ name: event.target.value })}
          helperText="Lowercase letters, numbers and hyphens."
        />
        {/* The one field that decides how the workspace is isolated from others,
            offered rather than hardcoded. The options come from the deployment's
            report intersected with the schema's accepted set, so nothing offered
            here can be rejected as an invalid mode. */}
        <Select
          name="workspace-isolation"
          label="Isolation mode"
          value={inputs.isolationMode}
          onChange={(event) =>
            update({ isolationMode: event.target.value as IsolationMode })
          }
          options={modes.map((mode) => ({ value: mode, label: ISOLATION_LABELS[mode] }))}
          helperText="How this workspace is separated from others. Research workspaces require a target account."
        />
        <Input
          name="workspace-account"
          label="Target account"
          value={inputs.account ?? ''}
          onChange={(event) => update({ account: event.target.value })}
          helperText={
            inputs.isolationMode === 'research'
              ? 'Required for research isolation.'
              : 'The cloud account the workspace is provisioned into.'
          }
          required={inputs.isolationMode === 'research'}
        />
        <Input
          name="workspace-region"
          label="Region"
          value={inputs.region ?? ''}
          onChange={(event) => update({ region: event.target.value })}
          helperText="Where the workspace is provisioned. Shown back to you in the plan before you confirm."
        />
        {/* Budget caps are how a user bounds what this can spend before it starts
            spending it. Left absent rather than defaulted to a number: the domain
            applies its own research defaults, and inventing a cap here would
            either override that silently or imply a limit the server never set. */}
        <Input
          name="workspace-budget-daily"
          label="Daily spend cap (USD)"
          type="number"
          min={0}
          value={inputs.budgetMaxDailyUsd === undefined ? '' : String(inputs.budgetMaxDailyUsd)}
          onChange={(event) => update({ budgetMaxDailyUsd: numberOrUndefined(event.target.value) })}
          helperText="Optional. Left blank, the domain applies its own guardrail for research workspaces and none otherwise."
        />
        <Input
          name="workspace-budget-gpus"
          label="Maximum GPUs"
          type="number"
          min={0}
          value={inputs.budgetMaxGpus === undefined ? '' : String(inputs.budgetMaxGpus)}
          onChange={(event) => update({ budgetMaxGpus: numberOrUndefined(event.target.value) })}
          helperText="Optional. A cap on concurrent GPUs this workspace may hold."
        />
      </div>

      {refusals.length > 0 && inputs.name.trim() !== '' && (
        <div className="mt-4">
          {/* Stated here rather than left to arrive as a 422, and stated as the
              server's rule rather than as a client preference — the user can act
              on the first and can only guess at the second. */}
          <Alert variant="warning" title="These inputs cannot be submitted yet">
            <ul className="list-disc space-y-1 pl-5">
              {refusals.map((refusal) => (
                <li key={refusal}>{refusal}</li>
              ))}
            </ul>
          </Alert>
        </div>
      )}

      {stage.name === 'blocked' && (
        <div id="superplane-create-problem" tabIndex={-1} role="group" aria-label="Workspace submission problem" className="mt-4">
          <Alert variant="error" title="This cannot be submitted in this environment">
            {stage.unavailable.detail}
          </Alert>
        </div>
      )}

      {stage.name === 'conflict' && (
        <div id="superplane-create-problem" tabIndex={-1} role="group" aria-label="Workspace submission problem" className="mt-4">
          <Alert variant="warning" title="A different submission is already in progress">
            {stage.detail}
          </Alert>
        </div>
      )}

      {stage.name === 'planning' && (
        <div className="mt-4 flex items-center gap-3">
          <Spinner />
          <p className="text-sm text-gray-600 dark:text-gray-400">Validating these inputs…</p>
        </div>
      )}

      {stage.name === 'reviewing' && !planIsStale && (
        <PlanReview plan={stage.plan} />
      )}

      {planIsStale && (
        <div className="mt-4">
          {/* The inputs moved after the plan was produced. Submitting the old plan
              under the new confirmation would build something the user never saw. */}
          <Alert variant="info" title="Inputs changed since this plan was produced">
            Review the plan again before submitting, so what you confirm is what
            gets built.
          </Alert>
        </div>
      )}

      {stage.name === 'reviewing' && stage.plan.approvalRequired && !approval && (
        <Button disabled={approvalBusy} onClick={() => void askApproval(stage.plan)}>Request approval for this plan</Button>
      )}
      {approval && <ApprovalPanel approval={approval} guard={guard} onChange={setApproval} />}
      {currentReceipt && currentReceipt.submissionStage !== 'draft' && currentReceipt.submissionStage !== 'approval' && <Receipt receipt={currentReceipt} />}
      {currentReceipt && currentReceipt.submissionStage !== 'draft' && currentReceipt.submissionStage !== 'approval' && <Button variant="secondary" onClick={() => void refreshReceipt()}>Refresh operation status</Button>}
      {recoveryProblem && <Alert variant="warning" title="Operation status unavailable">{recoveryProblem.detail}</Alert>}
      {ENDPOINTS.listLifecycleProposals.served && currentReceipt?.workspaceId && <LifecycleProposalPanel
        key={`${scope.orgId}:${currentReceipt.workspaceId}`}
        workspaceId={currentReceipt.workspaceId} scope={scope} store={store}
        mayManage onProgress={onCreated} section={section}
      />}

      <div className="mt-6 flex flex-col gap-2 sm:flex-row">
        {stage.name === 'reviewing' && !planIsStale ? (
          <Button
            onClick={() => void submit(stage.plan)}
            disabled={idempotencySupport !== null || (stage.plan.approvalRequired && (approval?.result !== 'allowed-once' || approval.revoked || !Number.isFinite(Date.parse(approval.expires_at)) || Date.parse(approval.expires_at) <= now))}
          >
            {inputs.mode === 'adopt' ? 'Adopt this cluster' : 'Create this workspace'}
          </Button>
        ) : (
          <Button
            onClick={() => void requestPlan()}
            disabled={
              refusals.length > 0 ||
              stage.name === 'planning' ||
              stage.name === 'submitting'
            }
            isLoading={stage.name === 'planning'}
          >
            Review plan
          </Button>
        )}
        {onCancel && (
          <Button variant="secondary" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>

      {idempotencySupport && (
        <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
          Submission is disabled because this environment has not confirmed it honours a
          submitted operation identity ({CREATE_IDEMPOTENCY_FEATURE}). Without that, a
          retried request could create a second workspace.
        </p>
      )}
    </section>
  );
}

/** Human labels for the schema's three isolation modes. */
const ISOLATION_LABELS: Record<IsolationMode, string> = {
  dedicated: 'Dedicated — its own cluster',
  namespace: 'Namespace — a namespace on a shared cluster',
  research: 'Research — dedicated, with budget guardrails and a required account',
};

/**
 * A number, or `undefined` for a field left blank.
 *
 * Blank must not become `0`: a zero daily cap is a real instruction that would
 * stop the workspace doing anything, while blank means "no opinion, apply your own
 * default". `NaN` is also `undefined` — a partially typed value is not a cap.
 */
function numberOrUndefined(value: string): number | undefined {
  if (value.trim() === '') return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

/**
 * The plan the user confirms.
 *
 * The cost estimate renders "not estimated" when the server has none, never as
 * `$0.00`. A zero is a claim about price; an absent estimate is an absence of one,
 * and a user who reads "$0.00" has been told something false about what this costs.
 */
function PlanReview({ plan }: { plan: OnboardingPlan }) {
  return (
    <div
      className="mt-4 rounded-lg border border-gray-200 p-4 dark:border-gray-700"
      aria-labelledby="superplane-plan-heading"
      role="group"
    >
      <h3
        id="superplane-plan-heading"
        tabIndex={-1}
        className="text-sm font-semibold text-gray-900 dark:text-white"
      >
        Review this plan
      </h3>
      <dl className="mt-3 space-y-2 text-sm">
        <Row label="Mode" value={plan.mode} />
        {plan.target.cluster && <Row label="Cluster" value={plan.target.cluster} />}
        <Row label="Account" value={plan.target.account ?? 'not specified'} />
        <Row label="Region" value={plan.target.region ?? 'not specified'} />
        <Row label="Ownership" value={plan.ownership} />
        <Row label="Requested capacity" value={plan.requestedCapacity} />
        <Row
          label="Estimated cost"
          value={
            plan.costEstimate
              ? `${plan.costEstimate.amountUsd} ${plan.costEstimate.currency} (as of ${plan.costEstimate.asOf})`
              : 'not estimated by the server'
          }
        />
        <Row label="Plan revision" value={plan.revision} />
      </dl>
      {plan.approvalRequired && (
        <div className="mt-3">
          <Alert variant="info" title="Approval required">
            This plan needs approval before it will be provisioned.
          </Alert>
        </div>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:justify-between sm:gap-4">
      <dt className="text-gray-500 dark:text-gray-400">{label}</dt>
      <dd className="font-medium break-words text-gray-900 dark:text-white">{value}</dd>
    </div>
  );
}

/**
 * The durable receipt.
 *
 * `unknown` is rendered as unknown, with its identity shown so the user can
 * correlate it with the server's records. AC-02 asks for durable progress; an
 * operation whose outcome was never observed is durable *progress*, not a failure,
 * and saying "failed" here is what produces the duplicate.
 */
function Receipt({ receipt }: { receipt: StoredReceipt }) {
  const unknown = receipt.state === 'unknown';
  const cancelled = receipt.state === 'cancelled';
  // Accepted and still building is the ordinary outcome of a create, not an edge
  // case: the server returns 201 with `status: "Provisioning"`. Rendering that as
  // a plain success told the user their workspace was ready while it was still
  // being built, and while it could still fail.
  const running = !unknown && !isTerminal(receipt.state);
  const title = cancelled ? 'Operation cancelled' : unknown
    ? 'Outcome not confirmed'
    : running
      ? 'Accepted — still being provisioned'
      : receipt.state === 'failed'
        ? 'The server reported this failed'
        : 'Operation completed';
  return (
    <div className="mt-4" role="status">
      <Alert
        variant={unknown || running || cancelled ? 'warning' : receipt.state === 'failed' ? 'error' : 'success'}
        title={title}
      >
        {cancelled ? 'The server confirmed cancellation. Resource cleanup is checked separately.' : unknown
          ? 'The submission was sent but no usable reply was seen, so its outcome is ' +
            'not known. The operation identity below is preserved — retrying these ' +
            'same inputs reuses it and cannot create a second workspace.'
          : running
            ? 'The server accepted the submission and is provisioning the workspace. ' +
              'It is not ready yet, and provisioning can still fail — this page keeps ' +
              'checking, and the operation identity below is preserved so nothing you ' +
              'do here can create a second workspace.'
            : receipt.state === 'failed'
              ? 'The server reported that provisioning this workspace failed. Nothing ' +
                'further was submitted.'
              : 'This operation completed. Review any next lifecycle plan; workspace readiness is checked separately.'}
        <span className="mt-2 block font-mono text-xs break-all">
          Operation identity: {receipt.idempotencyKey}
        </span>
      </Alert>
    </div>
  );
}

export default CreateWorkspaceFlow;
