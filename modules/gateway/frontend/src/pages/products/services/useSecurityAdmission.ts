import { useEffect, useRef, useState } from 'react';
import { matchesSecurityScope, mutateSecurityRun, submitSecurityRun, type SecurityConfiguration, type SecurityRun, type SecurityRunInput, type Versioned } from '@/services/l5Services';

export type PendingSecurityAdmission = { receipt: string } & (
  { kind: 'submit'; input: SecurityRunInput } | { kind: 'retry'; runId: string; reason: string; capability?: SecurityRunInput['capability'] }
);
interface Props {
  productId: string; projectId: string; configuration: Versioned<SecurityConfiguration> | null; selection: string;
  onAccepted: (run: Versioned<SecurityRun>) => void; onDenied: () => void;
}
interface Preparation { stage: string; paused: boolean; explanation?: string }
const stages: Record<string, string> = { queued: 'Waiting for snapshot capture.', capturing: 'Capturing the pinned repository revision.', finalizing: 'Verifying and finalizing the snapshot.' };
const preparationActions: Record<string, string> = {
  source_capture_failed: 'Repository preparation could not complete. Check source access and capture limits, then review the saved authorization before retrying.',
  source_expired: 'The pinned source has expired. Review the current source revision, source access and saved authorization before retrying.',
  source_preparation_unavailable: 'Repository preparation is unavailable. Ask the platform administrator to restore the source preparation worker, then retry the original receipt.',
};
const captureActions: Record<string, string> = {
  not_found: 'Source access changed during preparation. Recheck Product membership and the repository connection before retrying.',
  connector_unavailable: 'The repository connection could not complete capture. Check the GitHub App installation and its repository access before retrying.',
  immutable_source_required: 'The saved source could not be verified as a pinned commit. Refresh the repository connection and select the intended immutable revision.',
  unsupported_source_snapshot: 'The complete repository could not be captured within the supported archive or file limits. Review the source preflight details; no files were silently excluded.',
  source_integrity_mismatch: 'Repository bytes did not match the pinned source inventory. Recheck the source connection and revision before retrying; the snapshot was rejected.',
  source_capture_timed_out: 'Repository capture exceeded its transfer window. Ask the platform administrator to check GitHub connectivity and snapshot storage before retrying.',
  source_capture_deadline_or_attempt_limit: 'Repository preparation reached its original deadline or retry limit. Review source connectivity and the saved authorization before starting another run.',
};
const windowMs = 10 * 60_000;

function restore(key: string | null, tenant: string | undefined, project: string): { pending: PendingSecurityAdmission | null; error: string } {
  try {
    const raw = key ? sessionStorage.getItem(key) : null;
    if (!raw) return { pending: null, error: '' };
    const value = JSON.parse(raw) as PendingSecurityAdmission;
    if (!value || typeof value.receipt !== 'string' || !/^[A-Za-z0-9_-]{16,128}$/.test(value.receipt)) throw new Error();
    if (value.kind === 'submit') {
      const input = value.input;
      if (!input || !Number.isInteger(input.configuration_revision) || input.configuration_revision < 1 || typeof input.binding_id !== 'string' || !input.binding_id
        || !['code_review', 'threat_model'].includes(input.capability) || !Array.isArray(input.sources) || !input.sources.length
        || input.sources.some(source => source.tenant_id !== tenant || source.project_id !== project
          || [source.source_connection_id, source.record_id, source.record_revision].some(part => typeof part !== 'string' || !part)
          || source.record_type !== (input.capability === 'code_review' ? 'repository_commit' : 'document'))
        || (input.pr_base !== undefined && typeof input.pr_base !== 'string') || (input.pr_head !== undefined && typeof input.pr_head !== 'string')) throw new Error();
    } else if (value.kind !== 'retry' || typeof value.runId !== 'string' || !value.runId || typeof value.reason !== 'string' || !value.reason.trim()
      || (value.capability !== undefined && !['code_review', 'threat_model'].includes(value.capability))) throw new Error();
    return { pending: value, error: '' };
  } catch { return { pending: null, error: 'The saved assessment receipt could not be read. Do not start another assessment; refresh assessment status and ask the platform administrator to recover the original request.' }; }
}

export function useSecurityAdmission({ productId, projectId, configuration, selection, onAccepted, onDenied }: Props) {
  const config = configuration?.value;
  const storageKey = config?.scope.owner_id?.trim() ? `adp:security-admission:${JSON.stringify([config.scope.tenant_id, productId, projectId, config.configuration_id, config.scope.owner_id])}` : null;
  const [initial] = useState(() => restore(storageKey, config?.scope.tenant_id, projectId));
  const [pending, setPending] = useState(initial.pending);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initial.error);
  const [preparation, setPreparation] = useState<Preparation | null>(null);
  const epoch = useRef(0);
  const active = useRef<AbortController | null>(null);
  const pendingRef = useRef(pending);
  const callbacks = useRef({ onAccepted, onDenied });
  callbacks.current = { onAccepted, onDenied };
  useEffect(() => {
    setBusy(false);
    if (pendingRef.current) setPreparation(current => current ? { ...current, paused: true, explanation: 'Automatic checks stopped because the assessment context changed. The original request is retained.' } : current);
    return () => { epoch.current += 1; active.current?.abort(); active.current = null; };
  }, [configuration, selection, productId, projectId, storageKey]);

  function clear() {
    epoch.current += 1; active.current?.abort(); active.current = null;
    if (storageKey) { try { sessionStorage.removeItem(storageKey); } catch { /* Server authority remains decisive. */ } }
    pendingRef.current = null; setPending(null); setBusy(false); setPreparation(null);
  }

  async function submit(request: PendingSecurityAdmission) {
    if (active.current || initial.error || !configuration) return;
    // Persist before admission so an uncertain response or reload cannot create a new receipt.
    try { if (storageKey) sessionStorage.setItem(storageKey, JSON.stringify(request)); }
    catch { setError('The original assessment receipt could not be saved in this session. Restore session storage before starting or continuing the assessment.'); return; }
    pendingRef.current = request; setPending(request); setBusy(true); setError(''); setPreparation(null);
    const controller = new AbortController(); active.current = controller;
    const generation = ++epoch.current;
    const deadline = Date.now() + windowMs;
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, windowMs);
    const current = () => generation === epoch.current;
    const pause = () => setPreparation(previous => ({ stage: previous?.stage ?? '', paused: true,
      explanation: 'Automatic checks stopped after ten minutes. Server preparation may continue. Refresh assessment status or retry the original receipt; do not start another assessment.' }));
    try {
      while (current() && !controller.signal.aborted) {
        try {
          const accepted = request.kind === 'submit'
            ? await submitSecurityRun(productId, projectId, request.input, request.receipt, controller.signal)
            : await mutateSecurityRun(productId, projectId, request.runId, 'retry', request.reason, request.receipt, controller.signal);
          if (!current()) return;
          if (timedOut) { pause(); return; }
          if (!matchesSecurityScope(accepted.value.scope, config?.scope, productId, projectId)) throw { status: 403 };
          if (storageKey) { try { sessionStorage.removeItem(storageKey); } catch { /* Retrying the retained key remains safe. */ } }
          pendingRef.current = null; setPending(null); setPreparation(null);
          callbacks.current.onAccepted(accepted);
          return;
        } catch (failure) {
          if (!current()) return;
          if (timedOut) { pause(); return; }
          const response = failure as { status?: number; detail?: { code?: unknown; cause?: unknown; stage?: unknown; retry_after_seconds?: unknown } } | null;
          if ([401, 403, 404].includes(response?.status ?? 0)) { clear(); callbacks.current.onDenied(); return; }
          if (response?.status !== 503 || response.detail?.code !== 'source_capture_preparing') {
            setPreparation(null);
            const causeAction = response?.detail?.code === 'source_capture_failed' && typeof response.detail.cause === 'string'
              ? captureActions[response.detail.cause] : undefined;
            const action = typeof causeAction === 'string' ? causeAction
              : typeof response?.detail?.code === 'string' ? preparationActions[response.detail.code] : undefined;
            setError(typeof action === 'string'
              ? `${action} Refresh assessment status first: a run may already have been admitted. The original receipt is retained.`
              : response?.status === 503
              ? 'Security run admission is unavailable in this environment. No assessment completion is confirmed; keep the original receipt for a later retry.'
              : 'Admission was not confirmed. Do not submit another assessment; retry the original receipt or refresh the run list.');
            return;
          }
          const label = typeof response.detail.stage === 'string' ? stages[response.detail.stage] : undefined;
          const stage = typeof label === 'string' ? label : '';
          const codeReview = request.kind === 'submit' ? request.input.capability === 'code_review' : request.capability === 'code_review';
          setPreparation({ stage, paused: !codeReview, ...(!codeReview ? { explanation: 'Refresh assessment status or retry the original receipt to check this request.' } : {}) });
          if (!codeReview) return;
          if (Date.now() >= deadline) { pause(); return; }
          const hint = response.detail.retry_after_seconds;
          const delay = typeof hint === 'number' && Number.isFinite(hint) ? Math.min(30, Math.max(5, hint)) * 1000 : 5000;
          await new Promise<void>(resolve => {
            const done = () => { window.clearTimeout(timer); controller.signal.removeEventListener('abort', done); resolve(); };
            const timer = window.setTimeout(done, Math.min(delay, deadline - Date.now()));
            controller.signal.addEventListener('abort', done, { once: true });
          });
          if (timedOut || Date.now() >= deadline) { if (current()) pause(); return; }
        }
      }
    } finally {
      window.clearTimeout(timeout);
      if (current()) { active.current = null; setBusy(false); }
    }
  }
  return { pending, busy, error, preparation, submit, clear, blocked: Boolean(initial.error), durable: Boolean(storageKey) };
}
