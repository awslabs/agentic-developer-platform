import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button } from '@/components/ui';
import { call, isSuperseded, ScopeGuard } from './client';
import type { Unavailable } from './contract';
import type { ServingDeployment, WorkloadKind } from './workloads';

interface Observation {
  state: string;
  checkedAt: string;
  pods: Array<{ uid: string; phase: string; ready: boolean; restarts: number; exitCode: number | null }>;
  logs: string | null;
  logsPodUid: string | null;
  limited: boolean;
}
interface Props { workspaceId: string; row: ServingDeployment; kind: WorkloadKind }
const record = (raw: unknown): raw is Record<string, unknown> => typeof raw === 'object' && raw !== null && !Array.isArray(raw);

export function parseWorkloadObservation(raw: unknown, props: Props, selected: string | null): Observation | null {
  if (!record(raw) || raw.workspace_id !== props.workspaceId || raw.deployment_id !== props.row.deploymentId || raw.kind !== props.kind ||
      typeof raw.uid !== 'string' || (props.row.providerUid !== null && props.row.providerUid !== raw.uid) ||
      typeof raw.checked_at !== 'string' || !Number.isFinite(Date.parse(raw.checked_at)) ||
      !['unknown', 'pending', 'running', 'succeeded', 'failed', 'progressing', 'ready'].includes(String(raw.state)) ||
      !Array.isArray(raw.pods) || raw.pods.length > 32 || typeof raw.logs_truncated !== 'boolean') return null;
  const pods: Observation['pods'] = [];
  for (const pod of raw.pods) {
    if (!record(pod) || typeof pod.uid !== 'string' || !/^[a-zA-Z0-9-]{1,255}$/.test(pod.uid) ||
        !['Pending', 'Running', 'Succeeded', 'Failed', 'Unknown', ''].includes(String(pod.phase)) ||
        pods.some((item) => item.uid === pod.uid) || typeof pod.ready !== 'boolean' || typeof pod.restarts !== 'number' || !Number.isInteger(pod.restarts) || pod.restarts < 0 ||
        (pod.exit_code !== null && (typeof pod.exit_code !== 'number' || !Number.isInteger(pod.exit_code)))) return null;
    pods.push({ uid: pod.uid, phase: String(pod.phase), ready: pod.ready, restarts: pod.restarts, exitCode: pod.exit_code as number | null });
  }
  if (selected ? typeof raw.logs !== 'string' || raw.logs.length > 65536 || raw.logs_pod_uid !== selected || !pods.some((pod) => pod.uid === selected) : raw.logs !== null) return null;
  return { state: String(raw.state), checkedAt: raw.checked_at, pods, logs: selected ? raw.logs as string : null,
    logsPodUid: selected, limited: raw.logs_truncated };
}

export function WorkloadObservation(props: Props) {
  return <ObservationView key={`${props.workspaceId}:${props.row.deploymentId}:${props.row.providerUid}`} {...props} />;
}

function ObservationView(props: Props) {
  const [guard] = useState(() => new ScopeGuard());
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [value, setValue] = useState<Observation | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const serial = useRef(0);
  const refresh = useCallback(async () => {
    if (!props.row.deploymentId) return;
    const current = ++serial.current;
    const result = await call(guard, props.kind === 'batch' ? 'observeBatchJob' : 'observeDeployment',
      { workspace_id: props.workspaceId, [props.kind === 'batch' ? 'job_id' : 'dep_id']: props.row.deploymentId }, undefined,
      (raw) => parseWorkloadObservation(raw, props, selected), { logs: selected ? 'true' : 'false', ...(selected ? { pod_uid: selected } : {}) });
    if (isSuperseded(result) || current !== serial.current) return;
    if (result.ok) { setValue(result.value); setProblem(null); }
    else if ('unavailable' in result) {
      setProblem(result.unavailable);
      if (result.unavailable.reason === 'not-permitted') { setValue(null); setSelected(null); }
    }
  }, [guard, props.workspaceId, props.row.deploymentId, props.row.providerUid, props.kind, selected]);
  useEffect(() => () => guard.supersede(), [guard]);
  useEffect(() => {
    if (!open || problem?.reason === 'not-permitted') return;
    void refresh(); const timer = setInterval(() => void refresh(), 10000);
    return () => clearInterval(timer);
  }, [open, refresh, problem?.reason]);
  return <div className="mt-2 space-y-2">
    <Button variant="secondary" onClick={() => { if (open) void refresh(); else setOpen(true); }}>Inspect status and logs for {props.row.name}</Button>
    {open && problem && <Alert variant="warning" title="Workload observation unavailable">{problem.detail}{value ? ' Displayed observations may be stale.' : ''}</Alert>}
    {open && value && <div className="rounded border p-3 space-y-2">
      <p>Observed workload: {value.state}. Checked at {value.checkedAt}.</p>
      <p>Workload outcome does not confirm resource cleanup or cost reconciliation.</p>
      <ul>{value.pods.map((pod) => <li key={pod.uid}>Pod {pod.uid}: {pod.phase}; ready: {pod.ready ? 'yes' : 'no'}; restarts: {pod.restarts}{pod.exitCode !== null ? `; exit code: ${pod.exitCode}` : ''}</li>)}</ul>
      {value.pods.length > 0 && <label>Pod log window<select className="block w-full rounded border p-2" value={selected ?? ''} onChange={(event) => { setSelected(event.target.value || null); }}>
        <option value="">Select a recorded Pod</option>
        {value.pods.map((pod) => <option value={pod.uid} key={pod.uid}>{pod.uid}</option>)}
      </select></label>}
      {selected && value.logsPodUid === selected && value.logs !== null && <>
        <p>Latest log window, limited to 100 lines and 16 KiB before redaction.{value.limited ? ' Earlier output may be omitted.' : ''}</p>
        <pre aria-label={`Logs for ${props.row.name}`} className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-100 p-3 text-sm">{value.logs}</pre>
      </>}
    </div>}
  </div>;
}
