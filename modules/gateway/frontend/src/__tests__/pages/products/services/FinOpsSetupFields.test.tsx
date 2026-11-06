import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it } from 'vitest';
import FinOpsSetupFields from '@/pages/products/services/FinOpsSetupFields';

const connection = { tenant_id: 'tenant', project_id: 'project', source_connection_id: 'aws-connection', record_type: 'connection', record_id: 'aws-connection', record_revision: '3' };
const initial = { service_id: 'finops', enabled: false, capabilities: ['cost_inquiry'],
  limits: { max_cost_usd: 5, deadline_seconds: 600 }, triggers: [{ trigger_id: 'retained-trigger', enabled: false }],
  finops: { agent_mode: 'adopt', data_role_ref: 'retained-role', billing_scope: { account_ids: [], view_ref: 'retained-view' } } };
function Fixture({ raw = JSON.stringify(initial) }: { raw?: string }) {
  const [value, setValue] = useState(raw);
  return <><FinOpsSetupFields value={value} onChange={setValue} connections={[connection]} regions={['us-east-1']} /><output aria-label="Retained draft">{value}</output><button type="button">Leave field</button></>;
}

it('lets an operator enter multiple accounts normally and preserves advanced policy and scope values', async () => {
  const user = userEvent.setup(); render(<Fixture />);
  await user.type(screen.getByLabelText('Billing account IDs (one per line)'), '111122223333{Enter}444455556666{Enter}');
  expect(screen.getByLabelText('Billing account IDs (one per line)')).toHaveValue('111122223333\n444455556666\n');
  await user.click(screen.getByRole('button', { name: 'Leave field' }));
  const draft = JSON.parse(screen.getByLabelText('Retained draft').textContent!);
  expect(draft.finops.billing_scope).toEqual({ account_ids: ['111122223333', '444455556666'], view_ref: 'retained-view' });
  expect(draft.limits).toEqual(initial.limits);
  expect(draft.triggers).toEqual(initial.triggers);
  expect(draft.finops.data_role_ref).toBe('retained-role');
});

it('saves selected authorized references and the explicit override reason together', async () => {
  const user = userEvent.setup(); render(<Fixture />);
  await user.selectOptions(screen.getByLabelText('AWS connection'), '0');
  await user.type(screen.getByLabelText('Reason for connection or Region choice'), 'Approved billing scope');
  await user.type(screen.getByLabelText('AWS Region'), 'us-east-1');
  const draft = JSON.parse(screen.getByLabelText('Retained draft').textContent!);
  expect(draft.connection_ref).toEqual(connection);
  expect(draft.overrides).toEqual([
    { field: 'connection_ref', value: connection, reason: 'Approved billing scope' },
    { field: 'region', value: 'us-east-1', reason: 'Approved billing scope' },
  ]);
  expect(screen.getByRole('option', { name: 'Create a new agent — Not available yet' })).toBeDisabled();
});

it('does not erase malformed advanced edits by replacing them with a basic form default', () => {
  render(<Fixture raw="{invalid" />);
  expect(screen.getByText(/Correct the JSON in Advanced settings/)).toBeInTheDocument();
  expect(screen.getByLabelText('Retained draft')).toHaveTextContent('{invalid');
  expect(screen.queryByLabelText('AWS Region')).not.toBeInTheDocument();
});

it('recognizes a saved authorized connection regardless of JSON property order', () => {
  const reordered = Object.fromEntries(Object.entries(connection).reverse());
  render(<Fixture raw={JSON.stringify({ ...initial, connection_ref: reordered })} />);
  expect(screen.getByLabelText('AWS connection')).toHaveValue('0');
});
