import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button } from '@/components/ui';
import { call, isSuperseded, ScopeGuard } from './client';
import type { Unavailable } from './contract';
import type { ServingDeployment, WorkloadKind } from './workloads';

interface Props { workspaceId: string; row: ServingDeployment; kind: WorkloadKind }
interface Accounting {
  checkedAt: string; committed: string; cap: string | null; budgetState: string;
  operations: Array<{ action: string; operationId: string; approved: string; held: string; state: string; shared: string; consistent: boolean; updatedAt: string }>;
  resources: Array<{ kind: string; count: number }>;
}
const record = (raw: unknown): raw is Record<string, unknown> => typeof raw === 'object' && raw !== null && !Array.isArray(raw);
const money = (raw: unknown): raw is string => typeof raw === 'string' && /^(0|[1-9][0-9]{0,29})$/.test(raw);
const stamp = (raw: unknown): raw is string => typeof raw === 'string' && Number.isFinite(Date.parse(raw));
const states = ['reserved', 'confirmed', 'released', 'retained'];
function usd(micros: string) {
  const digits = micros.padStart(7, '0');
  const fraction = digits.slice(-6).replace(/0+$/, '');
  return `${digits.slice(0, -6)}${fraction ? '.' + fraction : ''} USD`;
}
function parse(raw: unknown, props: Props): Accounting | null {
  if (!record(raw) || raw.workspace_id !== props.workspaceId || raw.deployment_id !== props.row.deploymentId || raw.kind !== props.kind ||
    !stamp(raw.checked_at) || !money(raw.workspace_committed_budget_micros) || (raw.workspace_reservation_cap_micros !== null && !money(raw.workspace_reservation_cap_micros)) ||
    !['available', 'exhausted', 'unconfigured'].includes(String(raw.workspace_budget_state)) || raw.observed_cost_micros !== null || raw.estimated_cost_micros !== null || raw.cost_reconciliation !== 'unavailable' ||
    !Array.isArray(raw.operations) || raw.operations.length < 1 || raw.operations.length > 2 || !Array.isArray(raw.recorded_resources) || raw.recorded_resources.length > 4) return null;
  const operations: Accounting['operations'] = [];
  for (const op of raw.operations) {
    if (!record(op) || !['provision', 'teardown'].includes(String(op.action)) || operations.some((item) => item.action === op.action) ||
      typeof op.operation_id !== 'string' || !money(op.approved_max_cost_micros) || !money(op.budget_held_micros) ||
      !states.includes(String(op.budget_state)) || !states.includes(String(op.shared_reservation_state)) || typeof op.accounting_consistent !== 'boolean' || !stamp(op.updated_at)) return null;
    operations.push({ action: String(op.action), operationId: op.operation_id, approved: op.approved_max_cost_micros, held: op.budget_held_micros,
      state: String(op.budget_state), shared: String(op.shared_reservation_state), consistent: op.accounting_consistent, updatedAt: op.updated_at });
  }
  if (!operations.some((op) => op.action === 'provision')) return null;
  const resources: Accounting['resources'] = [];
  for (const item of raw.recorded_resources) {
    if (!record(item) || !['compute', 'storage', 'network', 'other'].includes(String(item.kind)) || resources.some((r) => r.kind === item.kind) ||
      typeof item.count !== 'number' || !Number.isSafeInteger(item.count) || item.count < 0) return null;
    resources.push({ kind: String(item.kind), count: item.count });
  }
  return { checkedAt: raw.checked_at, committed: raw.workspace_committed_budget_micros, cap: raw.workspace_reservation_cap_micros as string | null,
    budgetState: String(raw.workspace_budget_state), operations, resources };
}
export function WorkloadAccounting(props: Props) {
  return <AccountingView key={`${props.workspaceId}:${props.row.deploymentId}`} {...props} />;
}
function AccountingView(props: Props) {
  const [guard] = useState(() => new ScopeGuard());
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState<Accounting | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const serial = useRef(0);
  const refresh = useCallback(async () => {
    if (!props.row.deploymentId) return;
    const current = ++serial.current;
    const result = await call(guard, props.kind === 'batch' ? 'batchAccounting' : 'servingAccounting',
      { workspace_id: props.workspaceId, [props.kind === 'batch' ? 'job_id' : 'dep_id']: props.row.deploymentId }, undefined, (raw) => parse(raw, props));
    if (isSuperseded(result) || current !== serial.current) return;
    if (result.ok) { setValue(result.value); setProblem(null); }
    else if ('unavailable' in result) { setProblem(result.unavailable); if (result.unavailable.reason === 'not-permitted') setValue(null); }
  }, [guard, props.workspaceId, props.row.deploymentId, props.kind]);
  useEffect(() => () => guard.supersede(), [guard]);
  useEffect(() => {
    if (!open || problem?.reason === 'not-permitted') return;
    void refresh(); const timer = setInterval(() => void refresh(), 15000);
    return () => clearInterval(timer);
  }, [open, refresh, problem?.reason]);
  return <div className="mt-2 space-y-2">
    <Button variant="secondary" onClick={() => { if (open) void refresh(); else setOpen(true); }}>View budget for {props.row.name}</Button>
    {open && problem && <Alert variant="warning" title="Workload accounting unavailable">{problem.detail}{value ? ' Displayed accounting may be stale.' : ''}</Alert>}
    {open && value && <div className="rounded border p-3 space-y-2">
      <p>Budget records checked at {value.checkedAt}.</p>
      <p>Estimated cost: unavailable. Observed provider cost: unknown. Cost reconciliation: unavailable.</p>
      {value.operations.map((op) => <div key={op.operationId}>
        <p>{op.action === 'provision' ? 'Original workload' : 'Stop operation'}: approved ceiling {usd(op.approved)}; budget held {usd(op.held)}.</p>
        <p>Reservation: {op.state}; operation ledger: {op.shared}. Updated {op.updatedAt}.</p>
        {!op.consistent && <p role="status">Accounting acknowledgement is incomplete; the reservation remains conservatively held.</p>}
      </div>)}
      <p>Workspace committed budget: {usd(value.committed)}. Reservation cap: {value.cap === null ? 'unconfigured' : usd(value.cap)}. Capacity: {value.budgetState}.</p>
      <p>Reservations are spending limits, not measured charges. Releasing a reservation does not establish a zero provider bill.</p>
      <p>Recorded allocation members: {value.resources.length ? value.resources.map((r) => `${r.count} ${r.kind}`).join(', ') : 'none recorded'}. Historical membership does not prove present resources or complete cleanup.</p>
    </div>}
  </div>;
}
