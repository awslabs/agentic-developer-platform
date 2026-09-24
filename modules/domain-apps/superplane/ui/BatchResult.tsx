import { useEffect, useRef, useState } from 'react';
import { Alert, Button } from '@/components/ui';
import { call, isSuperseded, ScopeGuard } from './client';
import type { Unavailable } from './contract';
import type { ServingDeployment } from './workloads';

interface Props { workspaceId: string; row: ServingDeployment }
interface Result { content: string; capturedAt: string; podUid: string; redacted: boolean }
const record = (raw: unknown): raw is Record<string, unknown> => typeof raw === 'object' && raw !== null && !Array.isArray(raw);
function parse(raw: unknown, props: Props): { result: Result | null } | null {
  if (!record(raw) || raw.workspace_id !== props.workspaceId || raw.job_id !== props.row.deploymentId ||
      raw.operation_id !== (props.row.sourceOperationId ?? props.row.operationId) || raw.media_type !== 'text/plain') return null;
  if (raw.status === 'not_captured' && raw.result === null) return { result: null };
  const result = raw.result;
  if (raw.status !== 'retained' || !record(result) || typeof result.content !== 'string' ||
      new TextEncoder().encode(result.content).length > 16384 || typeof result.captured_at !== 'string' || !Number.isFinite(Date.parse(result.captured_at)) ||
      typeof result.pod_uid !== 'string' || !/^[a-zA-Z0-9-]{1,255}$/.test(result.pod_uid) ||
      typeof result.job_uid !== 'string' || (props.row.providerUid && result.job_uid !== props.row.providerUid) ||
      typeof result.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(result.sha256) || typeof result.redacted !== 'boolean') return null;
  return { result: { content: result.content, capturedAt: result.captured_at, podUid: result.pod_uid, redacted: result.redacted } };
}
export function BatchResult(props: Props) {
  return <ResultView key={`${props.workspaceId}:${props.row.deploymentId}:${props.row.operationId}:${props.row.providerUid}`} {...props} />;
}
function ResultView(props: Props) {
  const [guard] = useState(() => new ScopeGuard());
  const [value, setValue] = useState<{ result: Result | null } | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const serial = useRef(0);
  useEffect(() => () => guard.supersede(), [guard]);
  async function read(download = false) {
    const current = ++serial.current;
    const response = await call(guard, 'batchResult', { workspace_id: props.workspaceId, job_id: props.row.deploymentId! }, undefined, (raw) => parse(raw, props));
    if (isSuperseded(response) || current !== serial.current) return;
    if (!response.ok) {
      setValue(null);
      if ('unavailable' in response) setProblem(response.unavailable);
      return;
    }
    setValue(response.value); setProblem(null);
    if (download && response.value.result) {
      // Reauthorize each download. Output is a text attachment, never a supplied
      // URL, HTML page, inline script or executable filename.
      const url = URL.createObjectURL(new Blob([response.value.result.content], { type: 'text/plain;charset=utf-8' }));
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = `batch-${props.row.deploymentId}.txt`;
      anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  }
  return <div className="mt-2 space-y-2">
    <Button variant="secondary" onClick={() => void read()}>View result for {props.row.name}</Button>
    {problem && <Alert variant="warning" title="Batch result unavailable">{problem.detail}</Alert>}
    {value && !value.result && <p>No result has been captured. The job may still be running, or its image did not publish a supported text result.</p>}
    {value?.result && <div className="rounded border p-3 space-y-2">
      <p>Result retained at {value.result.capturedAt} from Pod {value.result.podUid}.</p>
      {value.result.redacted && <p>Credential patterns and control characters were removed.</p>}
      <pre className="whitespace-pre-wrap break-all max-h-80 overflow-y-auto" aria-label="Batch result text">{value.result.content}</pre>
      <Button variant="secondary" onClick={() => void read(true)}>Download text result</Button>
      <p>Retained output remains available after Job cleanup. It does not establish resource cleanup or provider cost.</p>
    </div>}
  </div>;
}
