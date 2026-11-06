import { apiClient } from './api';

export type NativeCredentialState = 'created' | 'awaiting_approval' | 'approved' | 'challenged' | 'unknown' | 'delivered' | 'denied' | 'expired' | 'acknowledged';
export interface NativeCredentialPreview {
  receipt_id: string;
  status: NativeCredentialState;
  expires_at: number;
  snapshot: string;
  approval_nonce: string;
  selection: {
    tenant_id: string; user_id: string; session_id: string;
    execution_id: string; request_id: string; receipt_id: string;
    credential_id: string; service: string; label: string;
    operation: 'assume_role' | 'materialize';
    argv: string[]; cwd: string; use_seconds: number;
  };
  offer: { command_digest: string; sink_digest: string; recipient_fingerprint: string };
}

const states: NativeCredentialState[] = ['created', 'awaiting_approval', 'approved', 'challenged', 'unknown', 'delivered', 'denied', 'expired', 'acknowledged'];
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const hex = /^[0-9a-f]{64}$/;
export function nativeReceiptId(value: string): string {
  if (!uuid.test(value)) throw new Error('invalid_native_credential_receipt');
  return value;
}

export function validateNativePreview(value: NativeCredentialPreview, receipt: string): NativeCredentialPreview {
  const selection = value?.selection;
  const offer = value?.offer;
  if (value?.receipt_id !== receipt || !states.includes(value.status)
      || !Number.isInteger(value.expires_at) || !hex.test(value.snapshot)
      || typeof value.approval_nonce !== 'string' || (value.status === 'awaiting_approval' && !value.approval_nonce)
      || !selection || selection.receipt_id !== receipt
      || !['assume_role', 'materialize'].includes(selection.operation)
      || !Array.isArray(selection.argv) || !selection.argv.length
      || selection.argv.some((v) => typeof v !== 'string' || v.includes('\0'))
      || !selection.argv[0].startsWith('/') || !selection.cwd?.startsWith('/')
      || !Number.isInteger(selection.use_seconds) || selection.use_seconds < 1 || selection.use_seconds > 900
      || (selection.operation === 'assume_role' && selection.use_seconds !== 900)
      || !offer || !hex.test(offer.command_digest) || !hex.test(offer.sink_digest) || !hex.test(offer.recipient_fingerprint)
      || ['tenant_id', 'user_id', 'session_id', 'execution_id', 'request_id', 'credential_id', 'service', 'label'].some(
        (key) => typeof selection[key as keyof typeof selection] !== 'string' || !selection[key as keyof typeof selection],
      )) throw new Error('invalid_native_credential_preview');
  // The page consumes only these metadata fields; arbitrary response fields,
  // including a mistaken credential-bearing field, never reach the UI.
  return {
    receipt_id: receipt, status: value.status, expires_at: value.expires_at,
    snapshot: value.snapshot, approval_nonce: value.approval_nonce,
    selection: {
      tenant_id: selection.tenant_id, user_id: selection.user_id, session_id: selection.session_id,
      execution_id: selection.execution_id, request_id: selection.request_id, receipt_id: receipt,
      credential_id: selection.credential_id, service: selection.service, label: selection.label,
      operation: selection.operation, argv: [...selection.argv], cwd: selection.cwd, use_seconds: selection.use_seconds,
    },
    offer: { command_digest: offer.command_digest, sink_digest: offer.sink_digest, recipient_fingerprint: offer.recipient_fingerprint },
  };
}

export async function getNativeCredentialPreview(receipt: string, signal?: AbortSignal): Promise<NativeCredentialPreview> {
  nativeReceiptId(receipt);
  const result = await apiClient.get<NativeCredentialPreview>(`/auth/native-credentials/approvals/${receipt}`, signal, true);
  return validateNativePreview(result, receipt);
}

export async function decideNativeCredential(preview: NativeCredentialPreview, action: 'approve' | 'deny'): Promise<NativeCredentialState> {
  nativeReceiptId(preview.receipt_id);
  const result = await apiClient.post<{ receipt_id: string; status: NativeCredentialState }>(
    `/auth/native-credentials/approvals/${preview.receipt_id}`,
    { snapshot: preview.snapshot, approval_nonce: preview.approval_nonce, action },
  );
  if (result?.receipt_id !== preview.receipt_id || result.status !== (action === 'approve' ? 'approved' : 'denied')) {
    throw new Error('unknown_native_credential_decision');
  }
  return result.status;
}
