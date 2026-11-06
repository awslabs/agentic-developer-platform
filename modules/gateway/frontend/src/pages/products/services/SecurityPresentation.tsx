import type { SecurityFindingLocation, SecurityResult } from '@/services/l5Services';
import './securityWorkspace.css';

export const securityCapabilityName = (capability?: string) => capability === 'threat_model' ? 'Threat modeling'
  : capability === 'code_review' ? 'Code scanning' : 'Security assessment';

export const securityDate = (value?: string) => {
  if (!value) return 'Not yet recorded';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC', timeZoneName: 'short',
  }).format(date);
};

export const securityResultName = (result: SecurityResult) => {
  const ref = result.native_job_ref;
  if (ref && 'threat_model_job_id' in ref || result.coverage_mode === 'documents') return 'Threat modeling';
  if (result.coverage_mode === 'full_snapshot') return 'Code scanning';
  const kind = ref && 'record_type' in ref ? ref.record_type : '';
  return kind.includes('threat') ? 'Threat modeling' : kind.includes('code') ? 'Code scanning' : 'Security assessment';
};

const severityOrder: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3, informational: 4 };
export const securitySeverityRank = (severity: string) => severityOrder[severity.toLowerCase()] ?? 5;

export const securityLocationLabel = (location: SecurityFindingLocation) => typeof location === 'string' ? location
  : `${location.filePath}${location.lineStart ? `:${location.lineStart}${location.lineEnd && location.lineEnd !== location.lineStart ? `–${location.lineEnd}` : ''}` : ''}`;

export function SecurityBadge({ value }: { value: string }) {
  const normalized = value.toLowerCase();
  const tone = ['succeeded', 'completed', 'complete', 'ready', 'supported', 'enabled'].includes(normalized) ? 'success'
    : ['critical', 'high', 'failed', 'expired'].includes(normalized) ? 'danger'
      : ['partial', 'medium', 'degraded', 'overdue', 'suspected_stalled', 'outcome_unknown'].includes(normalized) ? 'warning'
        : ['running', 'in_progress', 'collecting', 'queued', 'materializing', 'submitting', 'admitted'].includes(normalized) ? 'active' : 'neutral';
  return <span className={`security-badge security-badge--${tone}`}>{value.replace(/_/g, ' ')}</span>;
}
