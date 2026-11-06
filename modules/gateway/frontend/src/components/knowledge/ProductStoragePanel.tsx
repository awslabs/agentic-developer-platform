import { useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Input } from '@/components/ui/Input';
import { Modal } from '@/components/ui/Modal';
import { beginContextUpload, completeContextUpload, configureContextStorage, getContextStorage, transferContextFile,
  type ContextStorage } from '@/services/contextStorage';

function message(error: unknown) {
  const detail = (error as { detail?: string | { message?: string } })?.detail;
  return typeof detail === 'string' ? detail : typeof detail?.message === 'string' ? detail.message
    : error instanceof Error ? error.message : 'Could not update Product storage. Please retry.';
}

export function ProductStoragePanel({ productId, onChanged }: { productId: string; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  return <Card>
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h3 className="text-base font-semibold text-gray-900 dark:text-white">Storage</h3>
        <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">Upload documents to this Product’s storage and configure encryption.</p></div>
      <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>Open storage</Button>
    </div>
    {open && <StorageDialog key={productId} productId={productId} onChanged={onChanged} onClose={() => setOpen(false)} />}
  </Card>;
}

function StorageDialog({ productId, onClose, onChanged }: { productId: string; onClose: () => void; onChanged: () => void }) {
  const lifetime = useRef(new AbortController());
  const fileInput = useRef<HTMLInputElement>(null);
  const [settings, setSettings] = useState<ContextStorage | null>(null);
  const [mode, setMode] = useState<ContextStorage['encryption']>('shared');
  const [key, setKey] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [uploaded, setUploaded] = useState<string | null>(null);
  const [busy, setBusy] = useState('Loading storage…');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  async function load(controller = lifetime.current) {
    setBusy('Loading storage…'); setError('');
    try {
      const current = await getContextStorage(productId, controller.signal);
      if (controller.signal.aborted) return;
      setSettings(current); setMode(current.encryption); setKey(current.encryption === 'customer' ? current.kms_key_arn : '');
    } catch (failure) { if (!controller.signal.aborted) setError(message(failure)); }
    finally { if (!controller.signal.aborted) setBusy(''); }
  }

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void load(controller);
    return () => controller.abort();
    // Each dialog is keyed to a Product and owns its request lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save() {
    const controller = lifetime.current;
    if (!settings || busy || !settings.can_configure) return;
    setBusy('Saving encryption…'); setError(''); setSuccess('');
    try {
      const current = await configureContextStorage(productId, { revision: settings.revision, encryption: mode,
        ...(mode === 'customer' ? { kms_key_arn: key.trim() } : {}) }, controller.signal);
      if (!controller.signal.aborted) { setSettings(current); setSuccess('Encryption saved for new uploads and indexing. Existing files keep their encryption.'); }
    } catch (failure) { if (!controller.signal.aborted) setError(message(failure)); }
    finally { if (!controller.signal.aborted) setBusy(''); }
  }

  async function upload() {
    const controller = lifetime.current;
    if (!settings || busy || !settings.can_upload || (!file && !uploaded)) return;
    if (file && (file.size === 0 || file.size > settings.max_upload_bytes)) {
      setError(`Choose a document between 1 byte and ${Math.floor(settings.max_upload_bytes / 1024 / 1024)} MB.`); return;
    }
    setBusy(uploaded ? 'Finishing import…' : 'Uploading document…'); setError(''); setSuccess('');
    try {
      let uploadId = uploaded;
      if (!uploadId && file) {
        const receipt = await beginContextUpload(productId, file, controller.signal);
        if (controller.signal.aborted) return;
        await transferContextFile(receipt, file, controller.signal);
        if (controller.signal.aborted) return;
        uploadId = receipt.upload_id; setUploaded(uploadId); setBusy('Finishing import…');
      }
      if (!uploadId) return;
      const asset = await completeContextUpload(productId, uploadId, controller.signal);
      if (!controller.signal.aborted) {
        setUploaded(null); setFile(null);
        if (fileInput.current) fileInput.current.value = '';
        onChanged();
        setSuccess(asset.status === 'registered' ? 'Document saved. Open its Context entry to retry indexing.' : 'Document added to Product Context. Indexing is in progress.');
      }
    } catch (failure) { if (!controller.signal.aborted) setError(message(failure)); }
    finally { if (!controller.signal.aborted) setBusy(''); }
  }

  return <Modal isOpen onClose={onClose} title="Product Context storage" size="lg">
    <div className="space-y-5">
      <p className="text-sm text-gray-600 dark:text-gray-400">Files belong to this Product and are available to its team and platform admins.</p>
      {busy && <p role="status" className="text-sm text-gray-500">{busy}</p>}
      {error && <p role="alert" className="text-sm text-red-700 dark:text-red-300">{error}</p>}
      {success && <p role="status" className="text-sm text-green-700 dark:text-green-300">{success}</p>}
      <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => void load()}>Refresh storage</Button>
      {settings && <>
        <dl className="space-y-2 text-sm">
          <div><dt className="text-gray-500">Product location</dt><dd className="break-all text-gray-900 dark:text-white">{settings.location}</dd></div>
          <div><dt className="text-gray-500">Current encryption key</dt><dd className="break-all text-gray-900 dark:text-white">{settings.kms_key_arn}</dd></div>
        </dl>
        {settings.can_configure && <div className="space-y-3 border-t border-gray-200 pt-4 dark:border-gray-700">
          <label className="block text-sm text-gray-700 dark:text-gray-300">Encryption for new content
            <select className="mt-1 block w-full rounded-md border border-gray-300 bg-white px-3 py-2 dark:border-gray-600 dark:bg-gray-800" value={mode}
              disabled={!!busy} onChange={event => setMode(event.target.value as ContextStorage['encryption'])}>
              <option value="shared">Shared platform key</option>
              <option value="managed">Dedicated ADP-managed product key</option>
              <option value="customer">Customer-supplied product key</option>
            </select>
          </label>
          {mode === 'customer' && <Input id="product-context-kms-key" label="Product KMS key ARN" value={key} disabled={!!busy} onChange={event => setKey(event.target.value)} />}
          {mode !== 'shared' && <p className="text-xs text-gray-500">The key must already be configured for this Product by your platform administrator.</p>}
          <p className="text-xs text-gray-500">Changing this setting keeps existing files readable. It does not re-encrypt them.</p>
          <Button size="sm" variant="secondary" disabled={!!busy || mode === 'customer' && !key.trim()} onClick={() => void save()}>Save encryption</Button>
        </div>}
        {settings.can_upload && <div className="space-y-3 border-t border-gray-200 pt-4 dark:border-gray-700">
          <label className="block text-sm text-gray-700 dark:text-gray-300">Document
            <input ref={fileInput} aria-label="Document" type="file" accept={settings.extensions.join(',')} disabled={!!busy || !!uploaded}
              onChange={event => { setFile(event.target.files?.[0] ?? null); setError(''); setSuccess(''); }} className="mt-2 block w-full text-sm" />
          </label>
          <p className="text-xs text-gray-500">Up to {Math.floor(settings.max_upload_bytes / 1024 / 1024)} MB. Files are checked before indexing.</p>
          <Button size="sm" disabled={!!busy || (!file && !uploaded)} onClick={() => void upload()}>{uploaded ? 'Finish import' : 'Upload document'}</Button>
        </div>}
      </>}
    </div>
  </Modal>;
}
