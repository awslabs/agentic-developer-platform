import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ProvisioningSteps } from '@superplane-ui/ProvisioningProgress';
import { parseWorkspace } from '@superplane-ui/client';
import type { ProvisioningProgress } from '@superplane-ui/contract';

const progress: ProvisioningProgress = {
  steps: [
    { id: 'plan', label: 'Prepare deployment plan', state: 'completed' },
    { id: 'infrastructure', label: 'Provision EKS cluster', state: 'completed' },
    { id: 'configure', label: 'Configure workspace', state: 'paused' },
    { id: 'verify', label: 'Verify access and health', state: 'pending' },
  ],
  completed: 2, total: 4, current_step: 'configure',
  message: 'Deployment needs attention.', observed_at: '2026-10-14T12:00:00Z',
};

describe('deployment progress', () => {
  it('shows completed count, remaining steps and the stopped step', () => {
    render(<ProvisioningSteps progress={progress} />);
    expect(screen.getByText('2 of 4 steps complete · 2 remaining')).toBeInTheDocument();
    expect(screen.getByText('Needs attention')).toBeInTheDocument();
    expect(screen.getByText('Configure workspace').closest('li')).toHaveAttribute('aria-current', 'step');
    expect(screen.getByText('Not started')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('value', '2');
  });

  it('identifies retained progress when refresh is unavailable', () => {
    render(<ProvisioningSteps progress={progress} problem="Could not refresh." />);
    expect(screen.getByText(/Showing the last recorded progress/)).toBeInTheDocument();
  });

  it('rejects inconsistent counts instead of overstating completion', () => {
    const workspace = { id: 'workspace', name: 'Example', status: 'Provisioning', provisioning_progress: progress };
    expect(parseWorkspace(workspace)?.provisioning_progress?.completed).toBe(2);
    expect(parseWorkspace({ ...workspace, provisioning_progress: { ...progress, completed: 4 } })?.provisioning_progress).toBeNull();
  });
});
