import { checklistContribution, type RunRecord } from '@/utils/runRecord';
import './run-workspace.css';
export function RunRecordSummary({ record }: { record?: RunRecord }) {
  if (!record) return <aside className="run-record" aria-label="Retained run record"><h3 className="font-semibold">Run record</h3><p className="text-sm mt-3">Structured checklist history was not captured or could not be read for this run. The original transcript remains available.</p></aside>;
  const tasks = record.latest_checklist?.tasks;
  const contribution = checklistContribution(record);
  return <aside className="run-record" aria-label="Retained run record">
    <h3 className="font-semibold">Saved assignment checklist</h3>
    {tasks ? <>
      <p className="run-count">{tasks.filter(t => t.status === 'completed').length} of {tasks.length} checked</p>
      <ul aria-label="Saved tasks">{tasks.map(t => <li key={t.id}><span aria-label={t.status.replace('_', ' ')}>{t.status === 'completed' ? '☑' : t.status === 'in_progress' ? '◐' : '☐'}</span><span>{t.text}</span></li>)}</ul>
      {record.first_checklist && <section aria-label="Work observed in this run" className="border-t mt-4 pt-3 text-sm">
        <h4 className="font-semibold">Work observed in this run</h4>
        <p className="mt-2">{contribution.completedSinceFirst.length} checked after first observation</p>
        <p>{contribution.alreadyComplete.length} already checked at first observation</p>
        <p>{contribution.addedComplete.length} added and checked</p>
        <p>{contribution.remaining.length} still open · {contribution.removed.length} removed or renamed</p>
        <details className="mt-3"><summary>First observed checklist</summary><ul>{record.first_checklist.tasks.map(t => <li key={t.id}>{t.status === 'completed' ? '☑' : '☐'} {t.text}</li>)}</ul></details>
        {!!contribution.removed.length && <details><summary>Removed or renamed tasks</summary><ul>{contribution.removed.map(t => <li key={t.id}>{t.text}</li>)}</ul></details>}
      </section>}
    </> : <p className="text-sm mt-3">No assignment checklist was captured.</p>}
    <p className="text-xs text-gray-500 mt-3">Agent-reported, not proof of acceptance. The baseline is the first observed checklist, not necessarily dispatch. Task matching uses exact wording.</p>
    {record.history_truncated && <p className="text-xs mt-2">Earlier task transitions were truncated.</p>}
    <dl>
      <dt>Capture started</dt><dd>{record.started_at}</dd>
      <dt>Last observation</dt><dd>{record.captured_at}</dd>
      <dt>Capture closed</dt><dd>{record.capture_closed_at ?? 'Not captured; record may be incomplete'}</dd>
      <dt>Model / persona</dt><dd>{record.model} / {record.persona}</dd>
      <dt>Worker / region</dt><dd>{record.worker || 'Unavailable'} / {record.region || 'Unavailable'}</dd>
      <dt>Starting revision</dt><dd><code>{record.starting_revision ?? 'Not captured'}</code></dd>
      <dt>Last observed local commit</dt><dd><code>{record.saved_revision ?? 'Not captured'}</code></dd>
      <dt>SDK sessions</dt><dd>{record.session_ids.length ? record.session_ids.map(s => <div key={s}><code>{s}</code></div>) : 'Not captured'}</dd>
    </dl>
  </aside>;
}
