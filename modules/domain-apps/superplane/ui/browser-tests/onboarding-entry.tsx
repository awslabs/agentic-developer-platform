import { useState } from 'react';
import { createRoot } from 'react-dom/client';

import { ApprovalPanel } from '@superplane-ui/ApprovalPanel';
import { CreateWorkspaceFlow } from '@superplane-ui/CreateWorkspaceFlow';
import { LifecycleProposalPanel } from '@superplane-ui/LifecycleProposalPanel';
import { RetirementPanel } from '@superplane-ui/RetirementPanel';
import { ScopeGuard } from '@superplane-ui/client';
import type { OperationApproval } from '@superplane-ui/contract';
import { browserReceiptStore } from '@superplane-ui/operations';
import '@/index.css';

window.sessionStorage.setItem('cognito_access_token', 'browser-fixture');
const scope = { deploymentId: window.location.origin, orgId: 'fixture-org' };
const store = browserReceiptStore(window.localStorage);
const guard = new ScopeGuard();
const continuation = new URLSearchParams(window.location.search).has('continuation');
const initialApproval: OperationApproval = {
  approval_id: 'fixture-approval', workspace_id: '11111111-1111-4111-8111-111111111111',
  action: 'provision', result: 'pending', can_decide: true, expires_at: '2999-01-01T00:00:00Z',
  revoked: false, target: { account: 'example-account', region: 'example-region-1' },
  plan_digest: 'a'.repeat(64), envelope: {
    max_resource_units: 2, max_runtime_seconds: 900, max_cost_micros: 1_000_000,
  },
};

function Fixture() {
  const [creating, setCreating] = useState(false);
  const [approval, setApproval] = useState(initialApproval);
  return <main className="mx-auto max-w-5xl p-4 space-y-6">
    {continuation ? <LifecycleProposalPanel workspaceId={initialApproval.workspace_id}
      scope={scope} store={store} mayManage /> : <>
    <button type="button" onClick={() => setCreating(true)}>Begin onboarding</button>
    {creating && <CreateWorkspaceFlow guard={guard} scope={scope} store={store}
      idempotencySupport={null}
      capabilities={{ features: ['create-operation-id-v1'], modes: ['managed'], providers: ['aws'], isolationModes: ['dedicated'] }} />}
    <ApprovalPanel approval={approval} guard={guard} onChange={setApproval} />
    </>}
    <RetirementPanel workspaceId={initialApproval.workspace_id} scope={scope} store={store}
      guard={guard} sessionToken="browser-fixture" />
  </main>;
}

createRoot(document.getElementById('root')!).render(<Fixture />);
