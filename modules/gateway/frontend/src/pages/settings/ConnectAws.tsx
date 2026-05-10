/**
 * Connect AWS Account page — CloudFormation Quick-Create flow.
 *
 * Issue #562: Self-serve AWS account connect UI.
 *
 * URL: /settings/credentials/aws/connect
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  startAwsConnect,
  verifyAwsConnect,
  type ConnectStartResponse,
} from '@/services/credentials';

export default function ConnectAws() {
  const navigate = useNavigate();
  const [nickname, setNickname] = useState('');
  const [accountId, setAccountId] = useState('');
  const [roleName, setRoleName] = useState('ADP-Agent-Role');
  const [isStarting, setIsStarting] = useState(false);
  const [isVerifying, setIsVerifying] = useState(false);
  const [pendingCredentialId, setPendingCredentialId] = useState<string | null>(null);
  const [launchUrl, setLaunchUrl] = useState<string | null>(null);
  const [verifyResult, setVerifyResult] = useState<{ status: string; reason?: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Client-side validation
  const isAccountIdValid = /^\d{12}$/.test(accountId);
  const isNicknameValid = nickname.trim().length > 0 && nickname.trim().length <= 64;
  const canLaunch = isAccountIdValid && isNicknameValid && !isStarting;

  const handleLaunch = async () => {
    setError(null);
    setIsStarting(true);
    try {
      const resp: ConnectStartResponse = await startAwsConnect({
        nickname: nickname.trim(),
        account_id: accountId,
        role_name: roleName,
      });
      setPendingCredentialId(resp.credential_id);
      setLaunchUrl(resp.launch_url);
      // Open AWS Console in new tab
      window.open(resp.launch_url, '_blank');
    } catch (err: unknown) {
      const message = (err as { message?: string })?.message || 'Failed to start connect flow';
      setError(message);
    } finally {
      setIsStarting(false);
    }
  };

  const handleVerify = async () => {
    if (!pendingCredentialId) return;
    setError(null);
    setIsVerifying(true);
    setVerifyResult(null);
    try {
      const resp = await verifyAwsConnect({ credential_id: pendingCredentialId });
      setVerifyResult(resp);
      if (resp.status === 'verified') {
        // Redirect to credentials list after short delay
        setTimeout(() => navigate('/settings/credentials'), 1500);
      }
    } catch (err: unknown) {
      const message = (err as { message?: string })?.message || 'Verification failed';
      setError(message);
    } finally {
      setIsVerifying(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto p-6">
      <h1 className="text-2xl font-bold mb-6">Connect an AWS Account</h1>

      {/* Form */}
      <div className="space-y-4 mb-6">
        <div>
          <label htmlFor="nickname" className="block text-sm font-medium text-gray-700 mb-1">
            Nickname *
          </label>
          <input
            id="nickname"
            type="text"
            value={nickname}
            onChange={(e) => setNickname(e.target.value)}
            placeholder="e.g. prod-readonly"
            className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:ring-blue-500 focus:border-blue-500"
            maxLength={64}
            disabled={!!pendingCredentialId}
          />
          {nickname && !isNicknameValid && (
            <p className="mt-1 text-sm text-red-600">Nickname must be 1-64 characters</p>
          )}
        </div>

        <div>
          <label htmlFor="accountId" className="block text-sm font-medium text-gray-700 mb-1">
            AWS Account ID *
          </label>
          <input
            id="accountId"
            type="text"
            value={accountId}
            onChange={(e) => setAccountId(e.target.value.replace(/\D/g, '').slice(0, 12))}
            placeholder="123456789012"
            className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:ring-blue-500 focus:border-blue-500"
            maxLength={12}
            disabled={!!pendingCredentialId}
          />
          {accountId && !isAccountIdValid && (
            <p className="mt-1 text-sm text-red-600">AWS account IDs are 12 digits</p>
          )}
        </div>

        <div>
          <label htmlFor="roleName" className="block text-sm font-medium text-gray-700 mb-1">
            Role Name
          </label>
          <input
            id="roleName"
            type="text"
            value={roleName}
            onChange={(e) => setRoleName(e.target.value)}
            className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:ring-blue-500 focus:border-blue-500"
            disabled={!!pendingCredentialId}
          />
        </div>
      </div>

      {/* Info box */}
      <div className="bg-blue-50 border border-blue-200 rounded-md p-4 mb-6">
        <p className="text-sm text-blue-800">
          Clicking Launch opens AWS Console in a new tab. The template creates an IAM role
          with read-only permissions. You can attach more policies in AWS after creation.
        </p>
      </div>

      {/* Launch button */}
      {!pendingCredentialId && (
        <button
          onClick={handleLaunch}
          disabled={!canLaunch}
          className="w-full px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isStarting ? 'Starting...' : 'Launch CloudFormation Stack \u2192'}
        </button>
      )}

      {/* Post-launch: verify section */}
      {pendingCredentialId && (
        <div className="mt-6 space-y-4">
          <div className="bg-gray-50 border border-gray-200 rounded-md p-4">
            <p className="text-sm text-gray-700 mb-3">
              After the stack finishes creating in your AWS Console, click below to verify:
            </p>
            <button
              onClick={handleVerify}
              disabled={isVerifying}
              className="w-full px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isVerifying ? 'Verifying...' : "I've created the stack \u2014 Verify & Save"}
            </button>
          </div>

          {launchUrl && (
            <p className="text-xs text-gray-500">
              Didn&apos;t open?{' '}
              <a href={launchUrl} target="_blank" rel="noopener noreferrer" className="text-blue-600 underline">
                Open CloudFormation Console manually
              </a>
            </p>
          )}
        </div>
      )}

      {/* Verify result */}
      {verifyResult?.status === 'verified' && (
        <div className="mt-4 bg-green-50 border border-green-200 rounded-md p-4">
          <p className="text-sm text-green-800 font-medium">
            Verified! AWS account connected successfully. Redirecting...
          </p>
        </div>
      )}

      {verifyResult?.status === 'failed' && (
        <div className="mt-4 bg-yellow-50 border border-yellow-200 rounded-md p-4">
          <p className="text-sm text-yellow-800">
            {verifyResult.reason || 'Verification failed. Please check the stack status and try again.'}
          </p>
          <button
            onClick={handleVerify}
            className="mt-2 px-3 py-1 text-sm bg-yellow-100 border border-yellow-300 rounded hover:bg-yellow-200"
          >
            Retry Verify
          </button>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="mt-4 bg-red-50 border border-red-200 rounded-md p-4">
          <p className="text-sm text-red-800">{error}</p>
        </div>
      )}
    </div>
  );
}
