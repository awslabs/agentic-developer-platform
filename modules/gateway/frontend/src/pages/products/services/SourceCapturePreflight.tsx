import { useEffect, useRef, useState } from 'react';
import { getSecuritySourcePreflight, type SourceCapturePreflightResult } from '@/services/l5Services';

interface Props {
  productId: string;
  projectId: string;
  configurationId: string;
  configurationRevision: number;
  disabled?: boolean;
}

const number = (value: number) => value.toLocaleString('en-US');
const statusText = {
  within_limits: 'Repository sizes fit the checked capture limits',
  exceeds_limits: 'Source selection exceeds capture limits',
  unknown: 'Source size could not be verified',
  not_applicable: 'No repositories are selected in this saved setup',
};
const reasons: Record<string, string> = {
  source_file_count_exceeded: 'Selected file count exceeds the limit.',
  combined_source_file_count_exceeded: 'The saved repositories together exceed the file limit for one run.',
  combined_source_content_bytes_exceeded: 'The saved repositories together exceed the content byte limit for one run.',
  source_content_bytes_exceeded: 'Selected file content exceeds the byte limit.',
  source_archive_bytes_exceeded: 'The source archive, including file names and ZIP headers, exceeds the byte limit.',
  empty_source_selection: 'The saved exclusions leave no files to capture.',
  source_metadata_incomplete: 'The repository tree was incomplete or too large to inspect safely. No complete size estimate is available.',
  source_metadata_invalid: 'The repository tree could not be validated.',
  unsupported_source_entry: 'The selection includes an unsupported entry, such as a symbolic link or submodule.',
  invalid_exclusions: 'Review the saved exclusions. Use exact file paths or directory prefixes, without wildcards.',
  immutable_source_required: 'Verify the saved repository selection at its exact commit.',
  source_metadata_unavailable: 'Verify access to the selected GitHub repository, then check again.',
};

/** A saved-source diagnostic; never used as run admission or setup readiness. */
export default function SourceCapturePreflight({ productId, projectId, configurationId, configurationRevision, disabled }: Props) {
  const [result, setResult] = useState<SourceCapturePreflightResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  useEffect(() => {
    generation.current += 1;
    setResult(null); setError(''); setBusy(false);
    return () => { generation.current += 1; };
  }, [productId, projectId, configurationId, configurationRevision]);

  async function check() {
    const request = ++generation.current;
    setBusy(true); setError(''); setResult(null);
    try {
      const checked = await getSecuritySourcePreflight(productId, projectId, configurationRevision);
      if (request !== generation.current) return;
      if (checked.configuration_id !== configurationId || checked.configuration_revision !== configurationRevision
        || checked.advisory_only !== true || checked.content_captured !== false) throw new Error('Source selection changed');
      setResult(checked);
    } catch {
      if (request === generation.current) setError('Source size could not be checked. Refresh the saved setup, verify source access, and try again.');
    } finally {
      if (request === generation.current) setBusy(false);
    }
  }

  return <aside aria-label="Source capture limits" className="l5-source-preflight">
    <h3>Source capture limits</h3>
    <p>Check the saved repository revisions using file metadata. This does not capture file content or start an assessment.</p>
    <button type="button" className="product-text-button" disabled={disabled || busy} onClick={() => void check()}>
      {busy ? 'Checking source size…' : 'Check source size'}
    </button>
    {error && <p role="alert">{error}</p>}
    {result && <div>
      <p role="status">{statusText[result.status]}</p>
      <p>Checked configuration revision {result.configuration_revision}. Current capture limits: {number(result.limits.max_files)} files and {number(result.limits.max_bytes)} bytes per source archive.</p>
      {result.sources.length > 1 && <div>
        <h4>Combined saved repositories, if included in one run</h4>
        {result.combined_repositories.metadata_coverage === 'complete'
          ? <p>{number(result.combined_repositories.known_selected_files)} files and {number(result.combined_repositories.known_selected_bytes)} content bytes against the shared per-run limits.</p>
          : <p>Some repository metadata is unavailable. Complete combined counts could not be verified.</p>}
        {!!result.combined_repositories.reasons.length && <ul>{result.combined_repositories.reasons.map(reason => <li key={reason}>{reasons[reason]}</li>)}</ul>}
        <p>Individual snapshot archives total {number(result.combined_repositories.snapshot_archive_bytes)} bytes for the verified repositories. This sum is informational; the compressed native combined archive size is unknown without capturing content.</p>
      </div>}
      {result.sources.map((source, index) => <section key={index} aria-label={`Source ${source.source_ref.record_id}`}>
        <h4 style={{ overflowWrap: 'anywhere' }}>{source.source_ref.record_id} · {source.source_ref.record_revision}</h4>
        <p>{statusText[source.status]}</p>
        {source.metadata_coverage === 'complete' && <>
          <p>{number(source.selected_files ?? 0)} selected files · {number(source.selected_bytes ?? 0)} content bytes · {number(source.archive_bytes ?? 0)} archive bytes.</p>
          <p>{source.coverage_scope === 'selected_paths'
            ? `Partial repository selection: ${number(source.excluded_entries ?? 0)} entries are excluded. This size check covers only the selected paths.`
            : 'No saved exclusions. The size check covers the whole repository tree.'}</p>
          {!!source.exclusions?.length && <details><summary>Saved exclusions ({source.exclusions.length})</summary>
            <ul>{source.exclusions.map(path => <li key={path}><code>{path}</code></li>)}</ul>
          </details>}
        </>}
        {!!source.reasons.length && <ul>{source.reasons.map(reason => <li key={reason}>{reasons[reason] ?? 'Source metadata could not be verified.'}</li>)}</ul>}
        {!!source.oversized_files?.length && <details><summary>Files too large to capture individually ({source.oversized_file_count})</summary>
          <ul>{source.oversized_files.map(file => <li key={file.path}><code>{file.path}</code> · {number(file.bytes)} content bytes</li>)}</ul>
          {source.oversized_files_truncated && <p>Showing the 20 largest files. Additional oversized files remain.</p>}
        </details>}
        {source.status === 'exceeds_limits' && <p>Review an explicit smaller source selection or ask a platform maintainer to address the capture limit. Changing exclusions reduces assessment coverage; no changes have been applied.</p>}
      </section>)}
      <p>This is a size check, not a security assessment or permission to run. Run authorization may impose lower limits, and multiple sources in one run share cumulative limits. Document sources and native analysis coverage have not been checked.</p>
    </div>}
  </aside>;
}
