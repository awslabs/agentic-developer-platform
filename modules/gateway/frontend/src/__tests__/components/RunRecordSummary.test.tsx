import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { RunRecordSummary } from '@/components/RunRecordSummary';
import { checklistContribution, parseRunRecord, type RunRecord } from '@/utils/runRecord';
const at = '2026-10-05T05:00:00Z';
const id = (n: number) => String(n).padStart(24, '0');
const record: RunRecord = {
  version: 1, invocation_id: 'run-42', persona: 'reviewer', model: 'codex', repository: 'acme/repo', issue: 42,
  started_at: at, captured_at: at, session_ids: ['repair', 'inspect'], history_truncated: false, evidence: [], task_transitions: [],
  first_checklist: { at, tasks: [
    { id: id(1), text: 'Already done', status: 'completed' }, { id: id(2), text: 'Fix', status: 'pending' },
    { id: id(3), text: 'Old wording', status: 'pending' },
  ] },
  latest_checklist: { at, tasks: [
    { id: id(1), text: 'Already done', status: 'pending' }, { id: id(2), text: 'Fix', status: 'completed' },
    { id: id(4), text: 'New wording', status: 'completed' }, { id: id(5), text: 'Verify', status: 'in_progress' },
  ] },
};
const envelope = (r: unknown) => '<!-- adp-run-record:v1 ' + btoa(JSON.stringify(r)) + ' -->\n\n# Transcript';
describe('retained run records', () => {
  it('decodes only bounded, supported records belonging to the requested invocation', () => {
    expect(parseRunRecord(envelope(record), 'run-42')).toEqual(record);
    expect(parseRunRecord(envelope(record), 'other')).toBeUndefined();
    expect(parseRunRecord('Tool quoted this:\n' + envelope(record), 'run-42')).toBeUndefined();
    expect(parseRunRecord(envelope({ ...record, version: 2 }), 'run-42')).toBeUndefined();
    expect(parseRunRecord(envelope({ ...record, latest_checklist: { at, tasks: [{ id: id(1), text: 'Bad', status: 'done' }] } }), 'run-42')).toBeUndefined();
    expect(parseRunRecord('<!-- adp-run-record:v1 !!!! -->', 'run-42')).toBeUndefined();
  });
  it('does not count renamed or reopened tasks as completed work', () => {
    const progress = checklistContribution(record);
    expect(progress.completedSinceFirst.map(t => t.text)).toEqual(['Fix']);
    expect(progress.addedComplete.map(t => t.text)).toEqual(['New wording']);
    expect(progress.removed.map(t => t.text)).toEqual(['Old wording']);
    expect(progress.remaining.map(t => t.text)).toEqual(['Already done', 'Verify']);
    render(<RunRecordSummary record={record} />);
    expect(screen.getByText('1 checked after first observation')).toBeInTheDocument();
    expect(screen.getByText('2 still open · 1 removed or renamed')).toBeInTheDocument();
    expect(screen.getByText('Not captured; record may be incomplete')).toBeInTheDocument();
  });
  it('shows unavailable history for legacy runs without inventing completion counts', () => {
    render(<RunRecordSummary />);
    expect(screen.getByText(/Structured checklist history was not captured/)).toBeInTheDocument();
    expect(screen.queryByText(/0 of/)).not.toBeInTheDocument();
  });
});
