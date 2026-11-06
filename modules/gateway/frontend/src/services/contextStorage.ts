import { apiClient } from './api';

export interface ContextStorage {
  product_id: string;
  bucket: string;
  prefix: string;
  location: string;
  revision: number;
  encryption: 'shared' | 'managed' | 'customer';
  kms_key_arn: string;
  can_configure: boolean;
  can_upload: boolean;
  max_upload_bytes: number;
  extensions: string[];
}

export interface ContextUpload {
  product_id: string;
  upload_id: string;
  source_ref: string;
  url: string;
  headers: Record<string, string>;
  expires_in: number;
}

const path = (productId: string) => `/agent-context/products/${encodeURIComponent(productId)}/storage`;
function sameProduct<T extends { product_id: string }>(value: T, productId: string): T {
  if (value?.product_id !== productId) throw new Error('Could not confirm storage for this Product.');
  return value;
}

export async function getContextStorage(productId: string, signal?: AbortSignal) {
  return sameProduct(await apiClient.get<ContextStorage>(path(productId), signal), productId);
}

export async function configureContextStorage(productId: string, settings: { revision: number; encryption: ContextStorage['encryption']; kms_key_arn?: string }, signal?: AbortSignal) {
  return sameProduct(await apiClient.put<ContextStorage>(path(productId), settings, signal), productId);
}

export async function beginContextUpload(productId: string, file: File, signal: AbortSignal): Promise<ContextUpload> {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  signal.throwIfAborted();
  const sha256 = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  return sameProduct(await apiClient.post<ContextUpload>(`${path(productId)}/uploads`, {
    filename: file.name, content_length: file.size, sha256,
  }, signal), productId);
}

export async function transferContextFile(receipt: ContextUpload, file: File, signal: AbortSignal) {
  // No ADP Authorization header or cookies are sent to the signed S3 URL.
  const response = await fetch(receipt.url, { method: 'PUT', body: file, headers: receipt.headers,
    credentials: 'omit', redirect: 'error', signal });
  if (!response.ok) throw new Error('The document could not be uploaded. Check the storage configuration and retry.');
}

export async function completeContextUpload(productId: string, uploadId: string, signal?: AbortSignal) {
  return sameProduct(await apiClient.post<{ id: string; product_id: string; status: string }>(
    `${path(productId)}/uploads/${encodeURIComponent(uploadId)}/complete`, undefined, signal), productId);
}

export async function contextDocumentDownload(productId: string, assetId: string) {
  const result = sameProduct(await apiClient.post<{ product_id: string; asset_id: string; url: string }>(
    `${path(productId)}/assets/${encodeURIComponent(assetId)}/download`), productId);
  if (result.asset_id !== assetId) throw new Error('Could not confirm the requested document.');
  return result.url;
}
