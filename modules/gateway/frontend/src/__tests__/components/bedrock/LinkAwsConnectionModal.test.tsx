import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { LinkAwsConnectionModal } from '@/components/bedrock/LinkAwsConnectionModal';
import { getOrganizations } from '@/services/admin';
import { linkAwsConnection, listExistingAwsConnections, registerDestination } from '@/services/bedrockRouting';

vi.mock('@/services/admin', () => ({ getOrganizations: vi.fn() }));
vi.mock('@/services/bedrockRouting', () => ({
  listExistingAwsConnections: vi.fn(), linkAwsConnection: vi.fn(), registerDestination: vi.fn(),
}));
const connection = {
  credential_id: 'owner-connection', account_id: '123456789012', label: 'Team AWS',
  org_id: 'source', org_name: 'Original org', owner_scope: 'user', owner_name: 'AWS Owner',
  status: 'verified', selectable: true, reason: null,
};
const close = vi.fn();
const linked = vi.fn();
function show() { render(<LinkAwsConnectionModal onClose={close} onLinked={linked} />); }
async function select(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole('option', { name: /Team AWS/ });
  await user.selectOptions(screen.getByLabelText('AWS connection'), 'owner-connection');
  await user.selectOptions(screen.getByLabelText('Link to organization'), 'target');
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(listExistingAwsConnections).mockResolvedValue([connection]);
  vi.mocked(getOrganizations).mockResolvedValue({ items: [{ id: 'target', name: 'Team org' }], hasMore: false } as never);
  vi.mocked(linkAwsConnection).mockResolvedValue({ destination: {} } as never);
});

describe('Existing AWS connection linking', () => {
  it('links the selected connection to another org without launching CloudFormation', async () => {
    const user = userEvent.setup();
    show();
    await select(user);
    await user.click(screen.getByRole('button', { name: 'Verify & link' }));
    await waitFor(() => expect(linked).toHaveBeenCalled());
    expect(linkAwsConnection).toHaveBeenCalledWith('owner-connection', 'target');
    expect(registerDestination).not.toHaveBeenCalled();
    expect(close).toHaveBeenCalledOnce();
  });

  it('keeps selection and shows AWS owner remediation after probe refusal', async () => {
    vi.mocked(linkAwsConnection).mockRejectedValue({ detail: { message: 'Ask the AWS account administrator to update Bedrock permissions.' } });
    const user = userEvent.setup();
    show();
    await select(user);
    await user.click(screen.getByRole('button', { name: 'Verify & link' }));
    expect(await screen.findByText('Ask the AWS account administrator to update Bedrock permissions.')).toBeVisible();
    expect(screen.getByLabelText('AWS connection')).toHaveValue('owner-connection');
    expect(screen.getByLabelText('Link to organization')).toHaveValue('target');
    expect(close).not.toHaveBeenCalled();
    expect(linked).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Verify & link' })).toBeEnabled();
  });

  it('shows pending connections and refuses submission until verified', async () => {
    vi.mocked(listExistingAwsConnections).mockResolvedValue([{ ...connection, selectable: false, status: 'pending', reason: 'connection_not_verified' }]);
    const user = userEvent.setup();
    show();
    await select(user);
    expect(screen.getByText(/Ask the connection owner to finish setup/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Verify & link' })).toBeDisabled();
    expect(linkAwsConnection).not.toHaveBeenCalled();
  });

  it('distinguishes inventory failure from no AWS connections and supports retry', async () => {
    vi.mocked(listExistingAwsConnections).mockRejectedValueOnce(new Error('Inventory unavailable'));
    const user = userEvent.setup();
    show();
    expect(await screen.findByText('Inventory unavailable')).toBeVisible();
    expect(screen.queryByText(/No AWS connections found/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByRole('option', { name: /Team AWS/ });
    expect(screen.queryByText('Inventory unavailable')).not.toBeInTheDocument();
  });

  it('shows an actionable empty inventory', async () => {
    vi.mocked(listExistingAwsConnections).mockResolvedValue([]);
    show();
    expect(await screen.findByText(/No AWS connections found/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Verify & link' })).toBeDisabled();
  });

  it('loads organizations beyond the first page', async () => {
    vi.mocked(getOrganizations)
      .mockResolvedValueOnce({ items: [{ id: 'source', name: 'Source org' }], hasMore: true } as never)
      .mockResolvedValueOnce({ items: [{ id: 'target', name: 'Team org' }], hasMore: false } as never);
    const user = userEvent.setup();
    show();
    await select(user);
    expect(getOrganizations).toHaveBeenCalledWith({ page: 2, pageSize: 100 });
  });
});
