/**
 * CliAuth — approve a pending CLI sign-in (`bg-cognito-auth.sh login --web`).
 *
 * The CLI opens this page at /cli-auth?code=<user_code> after calling
 * POST /auth/cli/start. The signed-in user confirms the code matches the one
 * shown in their terminal and clicks Approve; the CLI's poll then receives
 * tokens minted on the CLI-specific app client. No credential ever appears
 * on screen — this page replaces the "Reveal refresh token and paste it"
 * flow for browser users.
 *
 * The code shown here is NOT a secret: it exists so the human can confirm
 * they are approving *their own* terminal's request, not someone else's.
 */

import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Card, CardTitle, Button, Alert } from '@/components/ui';
import { apiClient } from '@/services/api';

type Phase = 'confirm' | 'submitting' | 'approved' | 'denied' | 'error';

interface ApproveError {
  detail?: { error?: string; message?: string };
  message?: string;
}

function errorMessage(err: unknown): string {
  const e = err as ApproveError;
  const code = e?.detail?.error;
  if (code === 'unknown_code') {
    return 'No pending CLI sign-in with this code — it may have expired. Re-run login --web in your terminal and try again.';
  }
  if (code === 'already_decided') {
    return 'This CLI sign-in was already approved or denied. If that was not you, re-run login --web to start fresh.';
  }
  if (code === 'password_login_required') {
    return e?.detail?.message ?? 'Your account uses a Cognito password — use bg-cognito-auth.sh login in the terminal instead.';
  }
  return e?.detail?.message ?? e?.message ?? 'Something went wrong. Re-run login --web and try again.';
}

export default function CliAuth() {
  const [searchParams] = useSearchParams();
  const code = (searchParams.get('code') ?? '').toUpperCase();
  const [phase, setPhase] = useState<Phase>('confirm');
  const [error, setError] = useState<string>('');

  const decide = async (action: 'approve' | 'deny') => {
    setPhase('submitting');
    try {
      await apiClient.post('/auth/cli/approve', { user_code: code, action });
      setPhase(action === 'approve' ? 'approved' : 'denied');
    } catch (err) {
      setError(errorMessage(err));
      setPhase('error');
    }
  };

  if (!code) {
    return (
      <div className="max-w-xl mx-auto mt-8">
        <Card>
          <CardTitle>CLI sign-in</CardTitle>
          <Alert variant="warning" title="No sign-in code in this link">
            This page approves a sign-in started from your terminal. Run{' '}
            <code className="font-mono">bg-cognito-auth.sh login --web</code> there and let it open
            this page for you.
          </Alert>
        </Card>
      </div>
    );
  }

  return (
    <div className="max-w-xl mx-auto mt-8">
      <Card>
        <CardTitle>Approve CLI sign-in?</CardTitle>

        {phase === 'approved' && (
          <Alert variant="success" title="Approved — you're done here">
            Return to your terminal: the CLI is receiving its credentials now. You can close this
            tab.
          </Alert>
        )}

        {phase === 'denied' && (
          <Alert variant="info" title="Sign-in denied">
            The CLI request was rejected and cannot be used. If this was you by mistake, re-run{' '}
            <code className="font-mono">login --web</code> in your terminal.
          </Alert>
        )}

        {phase === 'error' && <Alert variant="error" title="Could not complete">{error}</Alert>}

        {(phase === 'confirm' || phase === 'submitting') && (
          <div className="mt-4 space-y-4 text-sm text-gray-700 dark:text-gray-300">
            <p>
              A command-line tool on your machine is asking to sign in to the gateway{' '}
              <strong>as you</strong>. Check that this code matches the one shown in your terminal:
            </p>

            <div
              data-testid="user-code"
              className="text-center text-3xl font-mono tracking-widest py-4 bg-gray-100 dark:bg-gray-800 rounded-lg text-gray-900 dark:text-white"
            >
              {code}
            </div>

            <Alert variant="warning" title="Only approve your own request">
              Approve only if you just ran <code className="font-mono">login --web</code> yourself
              and the codes match. If this appeared out of nowhere, deny it.
            </Alert>

            <div className="flex gap-3 justify-end">
              <Button variant="outline" disabled={phase === 'submitting'} onClick={() => decide('deny')}>
                Deny
              </Button>
              <Button disabled={phase === 'submitting'} onClick={() => decide('approve')}>
                {phase === 'submitting' ? 'Working…' : 'Approve sign-in'}
              </Button>
            </div>

            <p className="text-gray-500 dark:text-gray-400">
              Approving gives that terminal a short-lived credential for your account (it refreshes
              itself in the background and expires within a day of inactivity). No token is shown or
              copied anywhere.
            </p>
          </div>
        )}
      </Card>
    </div>
  );
}
