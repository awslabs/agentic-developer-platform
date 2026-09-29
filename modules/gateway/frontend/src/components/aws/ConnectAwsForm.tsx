/**
 * The AWS CloudFormation quick-create + verify flow, as a reusable component.
 *
 * Issue #4745 (#4692 · R4), §6.6 / ruling 4b: *"opens the SAME role-ARN form component
 * the credentials page uses (identical input + test-assume validation), saving into the
 * platform-scoped registry rather than a personal one."*
 *
 * **This is a move, not a copy.** The flow lived in `pages/settings/ConnectAws.tsx` as a
 * page — it owned a route, called `useNavigate()`, and held its own form state. Honouring
 * "the SAME component" therefore meant extracting it so both callers render one
 * implementation. §6.6 names the alternative as the failure mode to avoid: two divergent
 * CloudFormation quick-create flows would drift the moment a template version changes,
 * and §5.0/§5.0b both change one. The markup and validation below are the page's,
 * unchanged; `ConnectAws.tsx` is now a thin wrapper that supplies the personal save.
 *
 * **What differs between the two callers is only where the save lands** — a personal
 * `user_credentials` row versus the platform destination registry. That is `onLaunch` /
 * `onVerify`, two parameters, not a fork. Everything else (12-digit validation, launching
 * in a new tab, the disabled-while-pending inputs, the retry-verify affordance) is
 * identical because it is the same flow.
 *
 * **There is no template-version prop.** Which template a caller gets is decided
 * server-side by the endpoint it calls — `/auth/credentials/aws/connect` for the personal
 * path, the admin registration route for the routing path (v2, per §5.0b). A prop here
 * would look like the knob that controls it and control nothing.
 */

import { useEffect, useState, type ReactNode } from 'react';

/** What a launch produced: somewhere to send the admin, and a handle to verify later. */
export interface ConnectAwsLaunchResult {
  /**
   * The quick-create URL, or null when there is nothing to launch.
   *
   * Null is a real case — promoting an already-connected account has a proven role
   * already — and the component skips straight to the verify step for it.
   */
  launch_url: string | null;
  /**
   * Opaque id of the thing that was created: a credential id for the personal path, a
   * destination id for the routing registry. Passed back to `onVerify` unchanged.
   *
   * Held here rather than by the caller because "which pending thing am I verifying" is
   * this flow's own state — exactly what `pendingCredentialId` was in the page.
   */
  handle: string;
}

/** A verification verdict. Not an exception: "the role is not assumable" is an answer. */
export interface ConnectAwsVerifyResult {
  verified: boolean;
  reason?: string | null;
}

export interface ConnectAwsSetup {
  role_arn: string;
  region: string;
  launch_url: string;
  download_filename: string;
  download_base64: string;
}

export interface ConnectAwsFormProps {
  /** Start the flow. Rejecting surfaces as the form's error; it does not throw upward. */
  onLaunch: (input: { nickname: string; accountId: string; roleName?: string }) => Promise<ConnectAwsLaunchResult>;
  /** Verify what `onLaunch` created. Resolves to a verdict; rejects only on transport failure. */
  onVerify: (handle: string) => Promise<ConnectAwsVerifyResult>;
  /** Called once verification succeeds, so the caller can navigate or refetch. */
  onVerified?: (handle: string) => void;
  /**
   * Whether to offer the role-name input.
   *
   * The personal connect endpoint accepts `role_name`; the routing registration endpoint
   * does not — its template names the role `ADP-Agent-${Nickname}` and declares no
   * role-name parameter. So the field is hidden there rather than shown and ignored,
   * which would be a control that appears to configure something and does not (#4511).
   */
  showRoleName?: boolean;
  /** Extra fields a caller needs (the routing panel's "Link to org"). Rendered before the info box. */
  children?: ReactNode | ((locked: boolean) => ReactNode);
  /**
   * Whether the caller's own `children` fields are complete.
   *
   * Gates the launch button alongside this component's validation, so a caller with a
   * required extra field cannot launch without it.
   */
  extraFieldsValid?: boolean;
  /** Copy for the info box, which differs between a personal connection and a shared destination. */
  infoText?: string;
  /** Test id prefix, so two mounted instances stay distinguishable. */
  testIdPrefix?: string;
  /** Enables the portable administrator handoff as well as the console path. */
  onGetSetup?: (handle: string) => Promise<ConnectAwsSetup>;
  initialConnection?: { handle: string; nickname: string; accountId: string };
  onPrepared?: () => void;
}

function connectErrorMessage(err: unknown, fallback: string): string {
  const detail = (err as { detail?: string | { message?: string } })?.detail;
  return (typeof detail === 'string' ? detail : detail?.message) || (err as { message?: string })?.message || fallback;
}

export function ConnectAwsForm({
  onLaunch,
  onVerify,
  onVerified,
  showRoleName = false,
  children,
  extraFieldsValid = true,
  infoText = 'Clicking Launch opens AWS Console in a new tab. The template creates an IAM role with read-only permissions. You can attach more policies in AWS after creation.',
  testIdPrefix = 'connect-aws',
  onGetSetup,
  initialConnection,
  onPrepared,
}: ConnectAwsFormProps) {
  const [nickname, setNickname] = useState(initialConnection?.nickname ?? '');
  const [accountId, setAccountId] = useState(initialConnection?.accountId ?? '');
  const [roleName, setRoleName] = useState('ADP-Agent-Role');
  const [isStarting, setIsStarting] = useState(false);
  const [isVerifying, setIsVerifying] = useState(false);
  const [handle, setHandle] = useState<string | null>(initialConnection?.handle ?? null);
  const [setup, setSetup] = useState<ConnectAwsSetup | null>(null);
  const [launchUrl, setLaunchUrl] = useState<string | null>(null);
  const [verifyResult, setVerifyResult] = useState<ConnectAwsVerifyResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const resumeHandle = initialConnection?.handle;

  useEffect(() => {
    if (!resumeHandle || !onGetSetup) return;
    let cancelled = false;
    setIsStarting(true);
    onGetSetup(resumeHandle)
      .then((details) => {
        if (!cancelled) {
          setSetup(details);
          setLaunchUrl(details.launch_url);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(connectErrorMessage(err, 'Could not load the saved setup. Please retry.'));
      })
      .finally(() => { if (!cancelled) setIsStarting(false); });
    return () => { cancelled = true; };
  }, [resumeHandle, onGetSetup]);

  // Client-side validation, mirroring the server's. Not a substitute for it — the server
  // re-checks — but a 12-digit rule caught here is a form error rather than a 422 the
  // operator has to interpret.
  const isAccountIdValid = /^\d{12}$/.test(accountId);
  const isNicknameValid = onGetSetup ? /^[A-Za-z0-9-]{1,54}$/.test(nickname.trim()) : nickname.trim().length > 0 && nickname.trim().length <= 64;
  const canLaunch = isAccountIdValid && isNicknameValid && extraFieldsValid && !isStarting;

  const handleLaunch = async (mode: 'console' | 'download' = 'console') => {
    setError(null);
    setIsStarting(true);
    try {
      let preparedHandle = handle;
      let url = launchUrl;
      if (!preparedHandle) {
        const result = await onLaunch({
          nickname: nickname.trim(),
          accountId,
          roleName: showRoleName ? roleName : undefined,
        });
        preparedHandle = result.handle;
        url = result.launch_url;
        setHandle(preparedHandle);
        onPrepared?.();
      }
      if (onGetSetup) {
        const details = await onGetSetup(preparedHandle);
        setSetup(details);
        url = details.launch_url;
        if (mode === 'download') {
          const bytes = Uint8Array.from(atob(details.download_base64), (char) => char.charCodeAt(0));
          const downloadUrl = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }));
          const link = document.createElement('a');
          link.href = downloadUrl;
          link.download = details.download_filename;
          document.body.appendChild(link);
          link.click();
          link.remove();
          URL.revokeObjectURL(downloadUrl);
        }
      }
      setLaunchUrl(url);
      if (mode === 'console' && url) {
        window.open(url, '_blank', 'noopener,noreferrer');
      }
    } catch (err: unknown) {
      setError(connectErrorMessage(err, 'Failed to prepare AWS setup. Please retry.'));
    } finally {
      setIsStarting(false);
    }
  };

  const handleVerify = async () => {
    if (!handle) return;
    setError(null);
    setIsVerifying(true);
    setVerifyResult(null);
    try {
      const result = await onVerify(handle);
      setVerifyResult(result);
      if (result.verified) {
        onVerified?.(handle);
      }
    } catch (err: unknown) {
      setError(connectErrorMessage(err, 'Verification failed'));
    } finally {
      setIsVerifying(false);
    }
  };

  return (
    <div className="space-y-4" data-testid={`${testIdPrefix}-form`}>
      <div className="space-y-4">
        <div>
          <label htmlFor={`${testIdPrefix}-nickname`} className="block text-sm font-medium text-gray-700 mb-1">
            Nickname *
          </label>
          <input
            id={`${testIdPrefix}-nickname`}
            type="text"
            value={nickname}
            onChange={(e) => setNickname(e.target.value)}
            placeholder={onGetSetup ? 'e.g. sophos-bedrock' : 'e.g. prod-readonly'}
            className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:ring-blue-500 focus:border-blue-500"
            maxLength={onGetSetup ? 54 : 64}
            disabled={!!handle || isStarting}
          />
          {onGetSetup && <p className="mt-1 text-xs text-gray-500">Use up to 54 letters, numbers or hyphens. This names the AWS role.</p>}
          {nickname && !isNicknameValid && <p className="mt-1 text-sm text-red-600">{onGetSetup ? 'Use 1–54 letters, numbers or hyphens.' : 'Nickname must be 1-64 characters'}</p>}
        </div>

        <div>
          <label htmlFor={`${testIdPrefix}-account-id`} className="block text-sm font-medium text-gray-700 mb-1">
            AWS Account ID *
          </label>
          <input
            id={`${testIdPrefix}-account-id`}
            type="text"
            value={accountId}
            onChange={(e) => setAccountId(e.target.value.replace(/\D/g, '').slice(0, 12))}
            placeholder="123456789012"
            className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:ring-blue-500 focus:border-blue-500"
            maxLength={12}
            disabled={!!handle || isStarting}
          />
          {accountId && !isAccountIdValid && <p className="mt-1 text-sm text-red-600">AWS account IDs are 12 digits</p>}
        </div>

        {showRoleName && (
          <div>
            <label htmlFor={`${testIdPrefix}-role-name`} className="block text-sm font-medium text-gray-700 mb-1">
              Role Name
            </label>
            <input
              id={`${testIdPrefix}-role-name`}
              type="text"
              value={roleName}
              onChange={(e) => setRoleName(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:ring-blue-500 focus:border-blue-500"
              disabled={!!handle || isStarting}
            />
          </div>
        )}

        {typeof children === 'function' ? children(!!handle || isStarting) : children}
      </div>

      <div className="bg-blue-50 border border-blue-200 rounded-md p-4">
        <p className="text-sm text-blue-800">{infoText}</p>
      </div>

      {onGetSetup ? (
        <div className="space-y-3">
          {isAccountIdValid && <p className="text-sm">Sign in to AWS account <strong>{accountId}</strong> before creating the stack.</p>}
          <div className="rounded-md border border-gray-200 dark:border-gray-700 p-3">
            <p className="text-sm mb-2">Have permission to create IAM roles in AWS? Open the stack, create it, then return here.</p>
            <button type="button" onClick={() => handleLaunch('console')} disabled={isStarting || isVerifying || (!handle && !canLaunch)} data-testid={`${testIdPrefix}-launch`}
              className="w-full px-4 py-2 bg-blue-600 text-white rounded-md disabled:opacity-50">
              {isStarting ? 'Preparing…' : 'Create role in AWS'}
            </button>
          </div>
          <div className="rounded-md border border-gray-200 dark:border-gray-700 p-3">
            <p className="text-sm mb-2">Need an AWS administrator to create the role? Download the template, parameters and instructions to share with them.</p>
            <button type="button" onClick={() => handleLaunch('download')} disabled={isStarting || isVerifying || (!handle && !canLaunch)} data-testid={`${testIdPrefix}-download`}
              className="w-full px-4 py-2 border border-gray-300 rounded-md disabled:opacity-50">
              Download CloudFormation package
            </button>
          </div>
        </div>
      ) : !handle && (
        <button
          onClick={() => handleLaunch()}
          disabled={!canLaunch}
          data-testid={`${testIdPrefix}-launch`}
          className="w-full px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isStarting ? 'Starting...' : 'Launch CloudFormation Stack →'}
        </button>
      )}

      {handle && (
        <div className="space-y-4">
          {onGetSetup && (
            <div className="text-sm space-y-2">
              {setup && <>
                <p>Expected role ARN: <code className="break-all">{setup.role_arn}</code></p>
                <p>AWS region: {setup.region}</p>
              </>}
              <p>Setup is saved. You can close this window and return to this destination under Model Access → Bedrock account routing → Continue setup.</p>
            </div>
          )}
          <div className="bg-gray-50 border border-gray-200 rounded-md p-4">
            <p className="text-sm text-gray-700 mb-3">
              {onGetSetup ? `Once you or your AWS administrator has created the stack in account ${accountId}, verify it here. You do not need AWS permissions for this step.` : 'After the stack finishes creating in your AWS Console, click below to verify:'}
            </p>
            <p className="text-sm text-gray-700 mb-3">
              Ask your platform administrator to approve the role ARN for gateway access before verifying.
            </p>
            <button
              onClick={handleVerify}
              disabled={isVerifying || isStarting}
              data-testid={`${testIdPrefix}-verify`}
              className="w-full px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isVerifying ? 'Verifying...' : "I've created the stack — Verify & Save"}
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

      {verifyResult?.verified && (
        <div className="bg-green-50 border border-green-200 rounded-md p-4" data-testid={`${testIdPrefix}-verified`}>
          <p className="text-sm text-green-800 font-medium">Verified! AWS account connected successfully.</p>
        </div>
      )}

      {verifyResult && !verifyResult.verified && (
        <div className="bg-yellow-50 border border-yellow-200 rounded-md p-4" data-testid={`${testIdPrefix}-verify-failed`}>
          <p className="text-sm text-yellow-800">
            {verifyResult.reason === 'trust_verification_failed'
              ? 'AWS connection verification failed. Check the role trust policy and platform approval, then try again.'
              : verifyResult.reason || 'Verification failed. Please check the stack status and try again.'}
          </p>
          <button
            onClick={handleVerify}
            disabled={isVerifying || isStarting}
            className="mt-2 px-3 py-1 text-sm bg-yellow-100 border border-yellow-300 rounded hover:bg-yellow-200"
          >
            Retry Verify
          </button>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-md p-4" data-testid={`${testIdPrefix}-error`}>
          <p className="text-sm text-red-800">{error}</p>
        </div>
      )}
    </div>
  );
}
