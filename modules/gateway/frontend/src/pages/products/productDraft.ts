import type { ProductProfile } from '@/services/products';

export type ProductFields = { name: string; purpose: string; users: string };
export type FieldErrors = Partial<Record<keyof ProductFields, string>>;
export type PendingProductWrite = { receipt: string; fields: ProductFields; revision?: number; productId?: string; projectId?: string };

export function validateProductFields(fields: ProductFields): FieldErrors {
  const errors: FieldErrors = {};
  const users = fields.users.split(',').map((user) => user.trim());
  if (!fields.name.trim() || fields.name.trim().length > 200) errors.name = 'Enter a name of at most 200 characters.';
  if (!fields.purpose.trim() || fields.purpose.trim().length > 10000) errors.purpose = 'Enter a purpose of at most 10,000 characters.';
  if (users.length > 100 || users.some((user) => !user || user.length > 200) || new Set(users).size !== users.length) {
    errors.users = 'Enter distinct intended users, separated by commas (up to 100 names).';
  }
  return errors;
}

export function serverFieldErrors(error: unknown): FieldErrors {
  if ((error as { status?: number })?.status !== 422) return {};
  const detail = (error as { detail?: { field?: string; fields?: { field: string }[] } }).detail;
  const fields = [detail?.field, ...(detail?.fields ?? []).map((field) => field.field)];
  const errors: FieldErrors = {};
  for (const field of fields) {
    if (field?.endsWith('name')) errors.name = 'Check the product name.';
    if (field?.endsWith('purpose')) errors.purpose = 'Check the purpose.';
    if (field?.includes('intended_users')) errors.users = 'Check the intended users.';
  }
  return errors;
}

export function productDraftKey(userId: string | undefined, orgId: string | undefined, scope: string) {
  return userId && orgId ? `product-draft:${userId}:${orgId}:${scope}` : null;
}

export function readProductDraft(key: string | null): { fields: ProductFields; baseline?: ProductProfile; pending?: PendingProductWrite } | null {
  if (!key) return null;
  try {
    const stored = sessionStorage.getItem(key);
    if (!stored) return null;
    const value = JSON.parse(stored);
    if (typeof value?.fields?.name !== 'string' || typeof value.fields.purpose !== 'string' || typeof value.fields.users !== 'string') return null;
    if (value.baseline && (typeof value.baseline.product_id !== 'string' || typeof value.baseline.project_id !== 'string' || !value.baseline.product)) return null;
    if (value.pending && (typeof value.pending.receipt !== 'string' || !/^[A-Za-z0-9_-]{16,128}$/.test(value.pending.receipt) ||
      typeof value.pending.fields?.name !== 'string' || typeof value.pending.fields.purpose !== 'string' || typeof value.pending.fields.users !== 'string')) return null;
    return value;
  } catch {
    return null;
  }
  return null;
}

export function writeProductDraft(key: string | null, fields: ProductFields | null, baseline?: ProductProfile, pending?: PendingProductWrite) {
  if (!key) return;
  try {
    if (fields) sessionStorage.setItem(key, JSON.stringify({ fields, baseline, pending }));
    else sessionStorage.removeItem(key);
  } catch {
  }
}

export type PendingProjectWrite = { name: string; revision: number; receipt: string };
export type ProjectDraft = { name: string; baseline: string; revision: number; pending?: PendingProjectWrite };

export function readProjectDraft(key: string | null): ProjectDraft | null {
  if (!key) return null;
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? 'null');
    if (typeof value?.name !== 'string' || typeof value.baseline !== 'string' || !Number.isSafeInteger(value.revision)) return null;
    if (value.pending && (typeof value.pending.name !== 'string' || !Number.isSafeInteger(value.pending.revision) ||
      typeof value.pending.receipt !== 'string' || !/^[A-Za-z0-9_-]{16,128}$/.test(value.pending.receipt))) return null;
    return value;
  } catch {
    return null;
  }
}

export function writeProjectDraft(key: string | null, draft: ProjectDraft | null) {
  if (!key) return;
  try {
    if (draft) sessionStorage.setItem(key, JSON.stringify(draft));
    else sessionStorage.removeItem(key);
  } catch {
  }
}

export type PendingBindingWrite = { kind: 'create' | 'link' | 'unlink'; value: string; revision: number; receipt: string };

export function readBindingWrite(key: string | null): PendingBindingWrite | null {
  if (!key) return null;
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? 'null');
    return ['create', 'link', 'unlink'].includes(value?.kind) && typeof value.value === 'string' &&
      Number.isSafeInteger(value.revision) && value.revision > 0 && typeof value.receipt === 'string' &&
      /^[A-Za-z0-9_-]{16,128}$/.test(value.receipt) ? value : null;
  } catch {
    return null;
  }
}

export function writeBindingWrite(key: string | null, write: PendingBindingWrite | null) {
  if (!key) return;
  try {
    if (write) sessionStorage.setItem(key, JSON.stringify(write));
    else sessionStorage.removeItem(key);
  } catch {
  }
}
