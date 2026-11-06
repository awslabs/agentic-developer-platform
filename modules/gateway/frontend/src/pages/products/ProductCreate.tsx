import { useEffect, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { Button, Card, Input, Textarea } from '@/components/ui';
import { createProduct, getProduct, productPath, productReceipt } from '@/services/products';
import { productDraftKey, readProductDraft, serverFieldErrors, validateProductFields, writeProductDraft, type FieldErrors, type ProductFields, type PendingProductWrite } from './productDraft';
import './productWorkspace.css';

export default function ProductCreate() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const key = productDraftKey(user?.id, user?.orgId, 'new');
  const [fields, setFields] = useState<ProductFields>(() => readProductDraft(key)?.fields ?? { name: '', purpose: '', users: '' });
  const [pending, setPending] = useState<PendingProductWrite | null>(() => readProductDraft(key)?.pending ?? null);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    const restored = readProductDraft(key);
    setFields(restored?.fields ?? { name: '', purpose: '', users: '' });
    setPending(restored?.pending ?? null);
    setErrors({});
    setMessage('');
  }, [key]);

  useEffect(() => {
    if (saving || pending) return;
    const field = (['name', 'purpose', 'users'] as const).find((field) => errors[field]);
    if (field) document.getElementById(`product-create-${field}`)?.focus();
  }, [errors, pending, saving]);

  function change(fields: ProductFields) {
    setFields(fields);
    writeProductDraft(key, fields);
    setErrors({});
    setMessage('');
  }

  function showErrors(errors: FieldErrors) {
    setErrors(errors);
  }

  async function reconcile(write: PendingProductWrite) {
    if (saving) return;
    setSaving(true);
    setMessage('');
    try {
      let productId = write.productId;
      let projectId = write.projectId;
      if (!productId || !projectId) {
        const created = await createProduct(write.fields.name.trim(), write.fields.purpose.trim(),
          write.fields.users.split(',').map((user) => user.trim()), write.receipt);
        productId = created.product_id;
        projectId = created.project_id;
        const received = { ...write, productId, projectId };
        setPending(received);
        writeProductDraft(key, fields, undefined, received);
      }
      const readback = await getProduct(productId, projectId);
      if (readback.product_id !== productId || readback.project_id !== projectId ||
        readback.product.name !== write.fields.name.trim() || readback.product.purpose !== write.fields.purpose.trim() ||
        JSON.stringify(readback.product.intended_users) !== JSON.stringify(write.fields.users.split(',').map((user) => user.trim()))) {
        throw new Error('Product readback did not match the submitted information.');
      }
      writeProductDraft(key, null);
      setPending(null);
      navigate(productPath(productId, projectId));
    } catch (failure) {
      const error = failure as { status?: number; detail?: { code?: string } } | null;
      if (error?.status === 409 && error.detail?.code === 'team_assignment_required') {
        setPending(null);
        writeProductDraft(key, fields);
        setMessage('Choose a primary ADP team before creating a product. Your details are kept here.');
        return;
      }
      const invalid = serverFieldErrors(failure);
      if (Object.keys(invalid).length) {
        setPending(null);
        writeProductDraft(key, fields);
        showErrors(invalid);
      }
      setMessage(Object.keys(invalid).length
        ? 'Check the highlighted fields. Your input is still here.'
        : "We couldn't confirm the save. Your details are kept here. When you're online, check save status.");
    } finally {
      setSaving(false);
    }
  }

  function save(event: FormEvent) {
    event.preventDefault();
    if (saving || pending) return;
    const invalid = validateProductFields(fields);
    if (Object.keys(invalid).length) {
      showErrors(invalid);
      setMessage('Check the highlighted fields. Your input is still here.');
      return;
    }
    const write = { receipt: productReceipt(), fields: { ...fields } };
    setPending(write);
    writeProductDraft(key, fields, undefined, write);
    void reconcile(write);
  }

  return <main className="product-workspace space-y-6">
    <header className="space-y-2"><h1>Create product</h1>
      <p className="product-muted">Save the information you know now. No repository, budget or external resource is needed.</p>
    </header>
    <Card className="max-w-2xl">
    <form noValidate onSubmit={(event) => void save(event)} className="space-y-5">
      <div>
        <Input id="product-create-name" label="Name" value={fields.name} disabled={saving || !!pending} onChange={(event) => change({ ...fields, name: event.target.value })} aria-invalid={!!errors.name} aria-describedby={errors.name ? 'product-create-name-error' : undefined} maxLength={200} />
        {errors.name && <p id="product-create-name-error">{errors.name}</p>}
      </div>
      <div>
        <Textarea id="product-create-purpose" label="Purpose" value={fields.purpose} disabled={saving || !!pending} onChange={(event) => change({ ...fields, purpose: event.target.value })} aria-invalid={!!errors.purpose} aria-describedby={errors.purpose ? 'product-create-purpose-error' : undefined} maxLength={10000} />
        {errors.purpose && <p id="product-create-purpose-error">{errors.purpose}</p>}
      </div>
      <div>
        <Input id="product-create-users" label="Intended users" value={fields.users} disabled={saving || !!pending} onChange={(event) => change({ ...fields, users: event.target.value })} aria-invalid={!!errors.users} aria-describedby={errors.users ? 'product-create-users-error' : undefined} placeholder="e.g. Maintainers" />
        {errors.users && <p id="product-create-users-error">{errors.users}</p>}
      </div>
      <p className="product-muted">We’ll set up the product for you. You can add more details later.</p>
      {message && <p role="alert" aria-live="assertive">{message}</p>}
      {pending && <button type="button" className="underline" disabled={saving} onClick={() => void reconcile(pending)}>Check save status</button>}
      <div className="flex flex-wrap items-center gap-4"><Button type="submit" disabled={saving || !!pending}>Save product</Button><Link className="product-text-button py-2" to="/products">Cancel</Link></div>
    </form>
    </Card>
  </main>;
}
