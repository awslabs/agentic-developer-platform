/**
 * Connect AWS Account page — CloudFormation Quick-Create flow.
 *
 * Issue #562: Self-serve AWS account connect UI.
 *
 * URL: /settings/credentials/aws/connect
 *
 * The form and launch/verify flow moved to `components/aws/ConnectAwsForm.tsx` in #4745,
 * so that the routing admin panel renders the SAME component rather than a copy of it
 * (#4692 ruling 4b, §6.6 — two divergent quick-create flows would drift the moment a
 * template version changed). What is left here is what is genuinely this page's: the
 * route, the heading, the personal save, and the redirect back to the credentials list.
 */

import { useNavigate } from 'react-router-dom';
import { ConnectAwsForm } from '@/components/aws/ConnectAwsForm';
import { startAwsConnect, verifyAwsConnect } from '@/services/credentials';

export default function ConnectAws() {
  const navigate = useNavigate();

  return (
    <div className="max-w-2xl mx-auto p-6">
      <h1 className="text-2xl font-bold mb-6">Connect an AWS Account</h1>
      <p className="text-sm text-gray-700 mb-4">
        Personal connections are for AWS accounts separate from the accounts used by the ADP platform.
        If your agents need access to platform resources, ask your ADP platform administrator to arrange scoped access for the task.
      </p>

      <ConnectAwsForm
        showRoleName
        onLaunch={async ({ nickname, accountId, roleName }) => {
          const resp = await startAwsConnect({ nickname, account_id: accountId, role_name: roleName });
          return { launch_url: resp.launch_url, handle: resp.credential_id };
        }}
        onVerify={async (credentialId) => {
          const resp = await verifyAwsConnect({ credential_id: credentialId });
          return { verified: resp.status === 'verified', reason: resp.reason };
        }}
        // The page's own behaviour, not the flow's: the routing panel closes a modal and
        // refetches its destinations instead.
        onVerified={() => setTimeout(() => navigate('/settings/credentials'), 1500)}
      />
    </div>
  );
}
