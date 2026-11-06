import type { ValidationCheck } from '@/types/orchestration';

/** Show what needs attention first; keep successful checks and codes tucked away. */
export function DeliveryChecks({ checks }: { checks: ValidationCheck[] }) {
  if (!checks.length) return null;
  const attention = checks.filter(check => check.status !== 'passed');
  const passed = checks.filter(check => check.status === 'passed');
  return (
    <div className="mt-2 space-y-2 text-xs" aria-label="Engine checks">
      {attention.length > 0 && (
        <ul className="space-y-2" aria-label="Checks needing attention">
          {attention.map(check => (
            <li key={check.code} className="rounded border-l-2 border-amber-500 bg-amber-50 p-2 text-amber-950 dark:bg-amber-950/30 dark:text-amber-100">
              <p className="font-medium">{check.status === 'failed' ? 'Needs attention' : 'Waiting'} · {check.label}</p>
              <p className="mt-1">{check.detail}</p>
              {check.next_action && <p className="mt-1"><span className="font-medium">Next: </span>{check.next_action}</p>}
            </li>
          ))}
        </ul>
      )}
      <details className="text-gray-600 dark:text-gray-400">
        <summary className="cursor-pointer py-1 font-medium">Check details{passed.length > 0 ? ` · ${passed.length} passed` : ''}</summary>
        <ul className="mt-1 space-y-2" aria-label="All recorded checks">
          {checks.map(check => (
            <li key={check.code}>
              <p><span className="font-medium">{check.label}</span> · {check.status === 'passed' ? 'Passed' : check.status === 'failed' ? 'Needs attention' : 'Waiting'}</p>
              {check.status === 'passed' && <p>{check.detail}</p>}
              {check.technical_detail && <p className="break-words">{check.technical_detail}</p>}
              <code className="break-all text-[11px]">{check.code}</code>
              {check.observed_at && <p>Checked <time dateTime={check.observed_at}>{new Date(check.observed_at).toLocaleString()}</time></p>}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
