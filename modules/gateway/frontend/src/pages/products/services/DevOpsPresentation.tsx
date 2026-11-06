import './devopsWorkspace.css';

export const workloadName = (id: string) => id.includes(':function:') ? id.split(':function:')[1]
  : id.includes(':log-group:') ? id.split(':log-group:')[1].replace(/:\*$/, '') : id;

export const devOpsDate = (value?: string) => {
  if (!value) return 'Time not recorded';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? 'Time not recorded' : new Intl.DateTimeFormat(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC', timeZoneName: 'short',
  }).format(date);
};
export const devOpsCapability = (value?: string) => value === 'release_readiness' ? 'Release readiness'
  : value === 'release_verification' ? 'Release verification' : 'Investigation';
export function DevOpsBadge({ value }: { value: string }) {
  const state = value.toLowerCase();
  const tone = ['succeeded', 'completed', 'complete', 'ready'].includes(state) ? 'success'
    : ['failed', 'expired', 'error'].includes(state) ? 'danger'
      : ['running', 'collecting', 'queued', 'submitting', 'admitted'].includes(state) ? 'active'
        : ['blocked', 'needs_input', 'partial', 'outcome_unknown'].includes(state) ? 'warning' : 'neutral';
  return <span className={`devops-badge devops-badge--${tone}`}>{value.replace(/_/g, ' ')}</span>;
}
