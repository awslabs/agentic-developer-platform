import ReactMarkdown from 'react-markdown';
import { ArtifactVerificationError } from '@/services/api';
import remarkGfm from 'remark-gfm';
import { DevOpsBadge } from './DevOpsPresentation';
import { useEffect, useRef, useState } from 'react';
import { DevOpsReportError, readDevOpsInvestigationReport, type DevOpsReport } from '@/services/devopsInvestigationReport';
import type { DevOpsResult, SecurityScope } from '@/services/l5Services';

interface Props { productId: string; projectId: string; scope: SecurityScope; result: DevOpsResult; onAccessDenied: () => void }
const text = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const document = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
function decodeContent(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch { return value; }
}
function summaryContent(report: DevOpsReport, kind: 'investigation_summary' | 'mitigation_summary') {
  const newest = [...report.journal].sort((a, b) => String(b.createdAt ?? '').localeCompare(String(a.createdAt ?? '')));
  const entry = newest.find(item => item.recordType === `${kind}_md`) ?? newest.find(item => item.recordType === kind);
  return entry ? decodeContent(entry.content) : null;
}
function observations(report: DevOpsReport) {
  const seen = new Set<string>();
  return [...report.journal].sort((a, b) => String(b.createdAt ?? '').localeCompare(String(a.createdAt ?? ''))).flatMap<{ title?: string; content: unknown }>(entry => {
    const content = decodeContent(entry.content ?? entry);
    const kind = entry.recordType ?? (document(content) ? content.type : undefined);
    // Provider messages, metrics and native UI snapshots remain in the retained
    // evidence; the report presents findings rather than execution internals.
    if (['message', 'utilization', 'ui_investigation_summary', 'investigation_summary', 'investigation_summary_md',
      'mitigation_summary', 'mitigation_summary_md'].includes(String(kind))) return [];
    if (!document(content)) return [{ title: undefined, content }];
    if (typeof content.id === 'string') {
      if (seen.has(content.id)) return [];
      seen.add(content.id);
    }
    const visible = Object.fromEntries(Object.entries(content).filter(([key, value]) => !['title', 'id', 'type', 'plan_id', 'activity_id'].includes(key)
      && value !== null && value !== undefined
      && (!Array.isArray(value) || value.length > 0)));
    return [{ title: typeof content.title === 'string' ? content.title : undefined, content: visible }];
  });
}
function investigationQuestion(report: DevOpsReport) {
  const description = report.task.description ?? report.task.content;
  if (report.evidence.kind === 'devops_investigation' && typeof description === 'string') {
    try {
      const request: unknown = JSON.parse(description);
      if (request && typeof request === 'object' && 'question' in request && typeof request.question === 'string') return request.question;
    } catch { /* Native descriptions can also be plain text. */ }
  }
  return description;
}
function Content({ value }: { value: unknown }) {
  if (value === null || value === undefined) return null;
  if (Array.isArray(value)) return <ul className="list-disc space-y-2 pl-5">{value.map((item, index) => <li key={index}><Content value={item} /></li>)}</ul>;
  if (typeof value === 'object') return <dl className="space-y-2">{Object.entries(value).map(([key, item]) => <div key={key}>
    <dt className="font-medium">{key.replace(/([a-z])([A-Z])/g, '$1 $2').replace(/_/g, ' ')}</dt>
    <dd className="mt-1"><Content value={item} /></dd>
  </div>)}</dl>;
  return <div className="devops-report"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children }) => <span>{children}</span>, img: ({ alt }) => <span>{alt}</span> }}>{String(value)}</ReactMarkdown></div>;
}

export default function DevOpsInvestigationReport({ productId, projectId, scope, result, onAccessDenied }: Props) {
  const selection = JSON.stringify([productId, projectId, scope, result.result_id, result.run_id, result.revision,
    result.origin, result.native_job_ref, result.native_task_id, result.evidence_ref]);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const [state, setState] = useState<{ selection: string; busy?: boolean; report?: DevOpsReport; error?: string } | null>(null);
  useEffect(() => {
    generation.current += 1;
    controller.current?.abort();
    return () => { generation.current += 1; controller.current?.abort(); };
  }, [selection]);
  const current = state?.selection === selection ? state : null;
  const findings = current?.report ? observations(current.report) : [];
  const summary = current?.report ? summaryContent(current.report, 'investigation_summary') : null;
  const nextSteps = current?.report ? summaryContent(current.report, 'mitigation_summary') : null;

  async function read() {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    const request = ++generation.current;
    setState({ selection, busy: true });
    try {
      const report = await readDevOpsInvestigationReport(productId, projectId, scope, result, abort.signal);
      if (!abort.signal.aborted && request === generation.current) setState({ selection, report });
    } catch (failure) {
      if (abort.signal.aborted || request !== generation.current) return;
      const status = (failure as { status?: number })?.status;
      const denied = status === 401 || status === 403 || status === 404;
      setState({ selection, error: denied
        ? 'Report access is no longer available. Refresh DevOps results and check your access to this project.'
        : failure instanceof DevOpsReportError ? failure.message
          : failure instanceof ArtifactVerificationError
            ? `The retained report could not be read and verified. ${failure.message} Refresh results and retry. If this repeats, ask the platform administrator to check the stored artifact and response headers.`
          : 'The retained report could not be read and verified. Refresh results and retry; if this continues, ask the platform administrator to inspect the report evidence.' });
      if (denied) onAccessDenied();
    }
  }

  return <section aria-label="DevOps investigation report" className="devops-report space-y-3">
    <h4 className="text-base font-semibold">Investigation report</h4>
    <p>Read the retained native observations and operational recommendations for this result.</p>
    <button type="button" onClick={() => void read()} disabled={current?.busy}>Read investigation report</button>
    {current?.busy && <p role="status">Reading and verifying retained report…</p>}
    {current?.error && <p role="alert">{current.error}</p>}
    {current?.report && <>
      <div className="devops-panel-heading"><h4>Investigation question</h4><DevOpsBadge value={text(current.report.task.status) || 'unknown'} /></div>
      <Content value={investigationQuestion(current.report)} />
      {summary !== null && <details open><summary>Investigation summary</summary><Content value={summary} /></details>}
      {nextSteps !== null && <details open><summary>Suggested next steps</summary><Content value={nextSteps} /></details>}
      <details><summary>Investigation observations ({findings.length})</summary>
      {!findings.length && <p>No journal observations were retained in this report.</p>}
      <ol className="list-decimal space-y-4 pl-5">{findings.map((entry, index) => <li key={index}>
        {entry.title && <h5 className="font-semibold">{entry.title}</h5>}
        <Content value={entry.content} />
      </li>)}</ol></details>
      {(current.report.recommendations.length > 0 || nextSteps === null) && <h5 className="pt-2 font-semibold">Operational recommendations</h5>}
      {!current.report.recommendations.length && nextSteps === null && <p>No operational recommendations were retained in this report.</p>}
      {current.report.recommendations.map((entry, index) => <article key={index} className="devops-panel space-y-2">
        <h6 className="font-semibold">{typeof entry.title === 'string' ? entry.title : `Recommendation ${index + 1}`}</h6>
        {Boolean(entry.priority || entry.status) && <p>Priority: {text(entry.priority) || 'not reported'}; status: {text(entry.status) || 'not reported'}.</p>}
        <Content value={decodeContent(entry.content ?? entry)} />
      </article>)}
      <details><summary>Complete retained evidence</summary>
        <pre className="whitespace-pre-wrap break-words">{JSON.stringify(current.report.evidence, null, 2)}</pre>
      </details>
    </>}
  </section>;
}
