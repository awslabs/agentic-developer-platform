import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { CapabilitySummary, CompactServiceJourney, ProfileSetupSummary, ScheduleAvailability, ServiceJourney, ServiceSection, ServiceDisclosure } from '@/pages/products/services/L5ServiceJourney';

describe('shared service information', () => {
  it('combines setup and capabilities without losing drafts and reveals invalid hidden schedule fields', async () => {
    const previous = window.location.href;
    const submitted = vi.fn();
    try {
      window.history.replaceState(null, '', '#l5-security-setup');
      render(<CompactServiceJourney><ServiceJourney service="security">
        <ServiceSection service="security" step="setup"><form id="compact-form" onSubmit={event => { event.preventDefault(); submitted(); }}><button>Save setup</button><label>Region<input defaultValue="us-east-1" /></label></form></ServiceSection>
        <ServiceSection service="security" step="capabilities"><p>Capability selections</p></ServiceSection>
        <ServiceSection service="security" step="runs"><ServiceDisclosure title="Schedule editor"><label>Schedule interval<input form="compact-form" type="number" min={60} defaultValue={10} /></label></ServiceDisclosure></ServiceSection>
      </ServiceJourney></CompactServiceJourney>);
      expect(screen.getByText('Capability selections')).toBeVisible();
      expect(within(screen.getByRole('navigation')).queryByRole('link', { name: 'Capabilities' })).not.toBeInTheDocument();
      await userEvent.clear(screen.getByLabelText('Region')); await userEvent.type(screen.getByLabelText('Region'), 'us-west-2');
      await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
      expect(submitted).not.toHaveBeenCalled();
      await waitFor(() => expect(screen.getByLabelText('Schedule interval')).toHaveFocus());
      await userEvent.click(screen.getByRole('link', { name: 'Setup', exact: true }));
      expect(screen.getByLabelText('Region')).toHaveValue('us-west-2');
    } finally { window.history.replaceState(null, '', previous); }
  });

  it('shows actual saved values, pinned revisions and override provenance without claiming the latest Profile', () => {
    render(<ProfileSetupSummary productId="product/a" projectId="project/a" configuration={{ revision: 4, inherited_fields: ['purpose', 'classification', 'source_refs'], settings: {
      purpose: 'Order processing', classification: 'confidential', profile_ref: { record_id: 'product/a', record_revision: '3' },
      source_refs: [{ record_id: 'architecture.md', record_revision: 'sha-one' }], region: 'us-east-1',
      overrides: [{ field: 'region', reason: 'Approved staging region', value: 'us-east-1' }],
    } }} />);
    expect(screen.getByText(/Product profile revision 3/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View Product profile' })).toHaveAttribute('href', '/products/product%2Fa/projects/project%2Fa');
    const profile = within(screen.getByRole('region', { name: 'From Product profile' }));
    expect(profile.getByText('Order processing')).toBeVisible();
    expect(profile.getByText('confidential')).toBeVisible();
    expect(screen.getByRole('row', { name: /Sources architecture.md · revision sha-one Product profile/ })).toBeInTheDocument();
    const service = within(screen.getByRole('region', { name: 'Added for this service' }));
    expect(service.getByText('us-east-1')).toBeVisible();
    expect(service.getByText('Service override')).toBeVisible();
    expect(screen.getByText('region override: Approved staging region')).toBeInTheDocument();
    expect(screen.getByText(/Edits to the Product profile are not automatically applied/)).toBeInTheDocument();
  });

  it('removes inherited values when access to the configuration is removed', () => {
    const view = render(<ProfileSetupSummary productId="p" projectId="j" configuration={{ settings: { purpose: 'Private purpose' } }} />);
    expect(screen.getByText('Private purpose')).toBeInTheDocument();
    view.rerender(<ProfileSetupSummary productId="p" projectId="j" configuration={null} />);
    expect(screen.queryByText('Private purpose')).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('keeps pen testing unavailable even when stale options or selections include it', () => {
    render(<CapabilitySummary service="security" capabilities={['threat_model', 'pentesting']} selected={['threat_model', 'pentesting']} />);
    const pentesting = within(screen.getByRole('article', { name: 'Pen testing' }));
    expect(pentesting.getByText('Not available yet')).toBeInTheDocument();
    expect(pentesting.queryByRole('button')).not.toBeInTheDocument();
    expect(pentesting.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(within(screen.getByRole('article', { name: 'Code scanning' })).getByText('Not enabled for this Product')).toBeInTheDocument();
    expect(within(screen.getByRole('article', { name: 'Threat modeling' })).getByText('Selected · run readiness checked below')).toBeInTheDocument();
  });

  it.each([['devops', 'release_verification', 'Release verification'], ['devops', 'release_readiness', 'Release readiness'], ['devops', 'prevention', 'Prevention'], ['well_architected', 'resource_inspection', 'AWS resource inspection']] as const)('does not equate backend options with usable execution for %s %s', (service, capability, title) => {
    render(<CapabilitySummary service={service} capabilities={[capability]} selected={[capability]} />);
    expect(within(screen.getByRole('article', { name: title })).getByText('Not available yet')).toBeInTheDocument();
    expect(screen.queryByText('Selected · run readiness checked below')).not.toBeInTheDocument();
  });

  it('describes supported Well-Architected scheduling without implying that navigation starts work', () => {
    render(<ScheduleAvailability service="well_architected" />);
    expect(screen.getByText(/Daily UTC review schedules use the saved sources and a ready binding/)).toBeInTheDocument();
    expect(screen.getByText(/current provider readiness and owner consent/)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('keeps Transform schedules unavailable until the backend can admit them', () => {
    render(<ScheduleAvailability service="transform" />);
    expect(screen.getByText(/Scheduled transformations: Not available yet/)).toBeInTheDocument();
    expect(screen.queryByText(/Existing backend schedules/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('does not imply that monthly FinOps templates are an installed scheduler', () => {
    render(<ScheduleAvailability service="finops" />);
    expect(screen.getByText(/Scheduled inquiries: Not available yet/)).toBeInTheDocument();
    expect(screen.queryByText(/Existing backend schedules/)).not.toBeInTheDocument();
  });
  it('shows only the selected section while preserving drafts when sections or editors are closed', async () => {
    render(<ServiceJourney service="security">
      <ServiceSection service="security" step="setup"><ServiceDisclosure title="Edit setup"><label>Region<input defaultValue="us-east-1" /></label></ServiceDisclosure></ServiceSection>
      <ServiceSection service="security" step="runs"><p>Run history</p></ServiceSection>
    </ServiceJourney>);
    const editor = screen.getByText('Edit setup');
    const input = screen.getByLabelText('Region');
    expect(input).not.toBeVisible();
    await userEvent.click(editor);
    await userEvent.clear(input);
    await userEvent.type(input, 'us-west-2');
    await userEvent.click(editor);
    expect(input).not.toBeVisible();
    await userEvent.click(screen.getByRole('link', { name: 'Runs & schedules' }));
    expect(screen.queryByRole('region', { name: 'Setup' })).not.toBeInTheDocument();
    expect(screen.getByText('Run history')).toBeVisible();
    await userEvent.click(screen.getByRole('link', { name: 'Setup' }));
    await userEvent.click(editor);
    expect(screen.getByLabelText('Region')).toBe(input);
    expect(input).toBeVisible();
    expect(input).toHaveValue('us-west-2');
    expect(screen.getByText('Run history')).not.toBeVisible();
  });

  it('does not call a missing value inherited and keeps missing inputs in view', () => {
    render(<ProfileSetupSummary productId="p" projectId="j" configuration={{ inherited_fields: ['classification'], gaps: ['classification'], settings: {} }} />);
    expect(within(screen.getByRole('region', { name: 'From Product profile' })).getByText(/No saved values were inherited/)).toBeVisible();
    expect(screen.getByText('Set the data classification in the Product profile, then refresh this setup.')).toBeVisible();
    expect(screen.getByText('Saved references and profile details').closest('details')).not.toHaveAttribute('open');
  });

  it('opens an existing section deep link on arrival', () => {
    const previous = window.location.href;
    try {
      window.history.replaceState(null, '', '#l5-security-results');
      render(<ServiceJourney service="security">
        <ServiceSection service="security" step="setup">Setup fields</ServiceSection>
        <ServiceSection service="security" step="results">Retained results</ServiceSection>
      </ServiceJourney>);
      expect(screen.getByText('Retained results')).toBeVisible();
      expect(screen.getByText('Setup fields')).not.toBeVisible();
      expect(screen.getByRole('link', { name: 'Results' })).toHaveAttribute('aria-current', 'location');
    } finally {
      window.history.replaceState(null, '', previous);
    }
  });

  it('reveals and focuses an invalid form-associated field in a hidden section without submitting', async () => {
    const submitted = vi.fn();
    render(<ServiceJourney service="security">
      <ServiceSection service="security" step="setup"><form id="setup-form" onSubmit={event => { event.preventDefault(); submitted(); }}><button>Save setup</button></form></ServiceSection>
      <ServiceSection service="security" step="runs"><ServiceDisclosure title="Monitoring"><label>Poll interval<input form="setup-form" type="number" min={60} defaultValue={10} /></label></ServiceDisclosure></ServiceSection>
    </ServiceJourney>);
    const input = screen.getByLabelText('Poll interval');
    expect(input).not.toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    expect(submitted).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('Check the highlighted field before saving');
    expect(screen.getByRole('region', { name: 'Runs & schedules' })).toBeVisible();
    expect(input).toBeVisible();
    await waitFor(() => expect(input).toHaveFocus());
    await userEvent.click(screen.getByRole('link', { name: 'Setup' }));
    expect(screen.getByRole('region', { name: 'Setup' })).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    await waitFor(() => expect(input).toHaveFocus());
    await userEvent.clear(input);
    await userEvent.type(input, '120');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('link', { name: 'Setup' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));
    expect(submitted).toHaveBeenCalledOnce();
  });

});
