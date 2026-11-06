import { useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';
import type { NativeArrival } from '@/services/devopsKnowledgeEvidence';

export interface DevOpsObservation {
  flowId: string; challenge_id: string; snapshot_sha256: string; subject: 'run_submission' | 'native_ui';
  expires_at: string; receipt: string;
  snapshot: { selection: { product_id: string; project_id: string; binding_id: string; configuration_id: string; configuration_revision: number; native_hostname?: string } };
}
interface Props {
  productId: string; projectId: string; bindingId: string; configurationId: string; revision: number;
  runId: string | null; launchReceipt: string | null; onSelect: (value: DevOpsObservation | null) => void;
  onNativeArrival?: (value: NativeArrival | null) => void;
}

export default function DevOpsReleaseEvidence(props: Props) {
  const [flow, setFlow] = useState('');
  const [challenge, setChallenge] = useState('');
  const [receipt, setReceipt] = useState('');
  const [selected, setSelected] = useState<DevOpsObservation | null>(null);
  const [hostname, setHostname] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const { onSelect, onNativeArrival } = props;
  const generation = useRef(0);
  useEffect(() => { generation.current += 1; setSelected(null); setBusy(false); onSelect(null); onNativeArrival?.(null); return () => { generation.current += 1; }; }, [props.productId, props.projectId, props.bindingId, props.configurationId, props.revision, onSelect, onNativeArrival]);
  const path = (id: string) => `/orchestration/flows/${encodeURIComponent(flow)}/l5-observations/${encodeURIComponent(id)}`;
  async function select() {
    const request = ++generation.current;
    setBusy(true); setMessage(''); setSelected(null); onSelect(null);
    try {
      const value = await apiClient.get<DevOpsObservation & { service_id: string; observation_receipt: unknown }>(path(challenge));
      if (request !== generation.current) return;
      const scope = value.snapshot?.selection;
      if (value.service_id !== 'devops' || !['run_submission', 'native_ui'].includes(value.subject) || value.observation_receipt
        || !Number.isFinite(Date.parse(value.expires_at)) || Date.parse(value.expires_at) <= Date.now() || scope?.product_id !== props.productId || scope?.project_id !== props.projectId
        || scope?.binding_id !== props.bindingId || scope?.configuration_id !== props.configurationId || scope?.configuration_revision !== props.revision
        || (value.subject === 'run_submission' && !/^[A-Za-z0-9_-]{16,128}$/.test(receipt))) throw new Error();
      const current = { ...value, flowId: flow, receipt };
      setSelected(current); onSelect(current);
      setMessage(value.subject === 'run_submission' ? 'Use Submit DevOps run once, then record the observed submission below.' : 'Use Open native DevOps UI, sign in through ADP, then return here to record the hostname you actually saw.');
    } catch { if (request === generation.current) setMessage('This observation is unavailable, expired, already recorded or belongs to a different selection. Read its existing receipts before repeating work.'); }
    finally { if (request === generation.current) setBusy(false); }
  }
  async function record() {
    if (!selected) return;
    const request = generation.current;
    setBusy(true); setMessage('');
    try {
      await apiClient.post(`/orchestration/flows/${encodeURIComponent(selected.flowId)}/l5-observations/${encodeURIComponent(selected.challenge_id)}/observe`, {
        snapshot_sha256: selected.snapshot_sha256,
        ...(selected.subject === 'run_submission'
          ? { observation: 'submitted', submitted_run_id: props.runId }
          : { observation: 'displayed', displayed_native_hostname: hostname.trim(), launch_receipt_id: props.launchReceipt }),
      });
      if (request !== generation.current) return;
      if (selected.subject === 'native_ui' && onNativeArrival) {
        const observed = await apiClient.get<NativeArrival>(`/orchestration/flows/${encodeURIComponent(selected.flowId)}/l5-observations/${encodeURIComponent(selected.challenge_id)}`);
        if (request !== generation.current) return;
        const scope = observed.snapshot?.selection;
        if (observed.challenge_id !== selected.challenge_id || observed.service_id !== 'devops' || observed.subject !== 'native_ui'
          || scope?.product_id !== props.productId || scope?.project_id !== props.projectId || scope?.binding_id !== props.bindingId
          || scope?.configuration_id !== props.configurationId || scope?.configuration_revision !== props.revision
          || observed.observation_receipt?.provenance !== 'authenticated-human-observation'
          || observed.observation_receipt.observed?.observation !== 'displayed') throw new Error('Native arrival readback is unavailable');
        onNativeArrival({ ...observed, flowId: selected.flowId });
      }
      setMessage('Your observation was recorded. Native results and release acceptance are checked separately.');
      setSelected(null); onSelect(null);
    } catch { if (request === generation.current) setMessage('The observation was not confirmed. Read the existing challenge and its receipts before trying again; do not repeat the run or SSO launch.'); }
    finally { if (request === generation.current) setBusy(false); }
  }
  return <details>
    <summary>Release qualification observation</summary>
    <p>Attach an existing owner-prepared observation to your own UI action. This does not grant consent or mark the release complete.</p>
    <label>Delivery flow ID <input value={flow} onChange={event => setFlow(event.target.value)} disabled={Boolean(selected)} /></label>
    <label>Prepared observation ID <input value={challenge} onChange={event => setChallenge(event.target.value)} disabled={Boolean(selected)} /></label>
    <label>Original UI run receipt <input value={receipt} onChange={event => setReceipt(event.target.value)} disabled={Boolean(selected)} /></label>
    <button type="button" onClick={() => void select()} disabled={busy || Boolean(selected) || !flow || !challenge}>Attach prepared observation</button>
    {selected?.subject === 'native_ui' && <label>Native hostname I saw after SSO <input value={hostname} onChange={event => setHostname(event.target.value)} /></label>}
    {selected && <button type="button" onClick={() => void record()} disabled={busy || (selected.subject === 'run_submission' ? !props.runId : !props.launchReceipt || !hostname.trim())}>
      {selected.subject === 'run_submission' ? 'Record that I submitted this run in ADP' : 'Record my native DevOps arrival'}
    </button>}
    {message && <p role="status">{message}</p>}
  </details>;
}
