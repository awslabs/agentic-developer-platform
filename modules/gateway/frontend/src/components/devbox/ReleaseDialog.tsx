/**
 * ReleaseDialog — Issue #8094.
 *
 * Release terminates the machine; the API refuses the request without an explicit
 * `workspace_retention` of "retain" or "delete", so the confirm button stays
 * disabled until the owner has chosen one.
 */
import { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Modal, ModalFooter } from '@/components/ui/Modal';
import type { WorkspaceRetention } from '@/hooks/useDevBox';

export default function ReleaseDialog({ isOpen, onClose, onConfirm, busy = false }: {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: (retention: WorkspaceRetention) => void;
  busy?: boolean;
}) {
  const [choice, setChoice] = useState<WorkspaceRetention | null>(null);
  const close = () => { setChoice(null); onClose(); };

  return <Modal isOpen={isOpen} onClose={close} title="Release this Dev Box" size="md">
    <p className="text-sm">
      Releasing terminates the machine. Choose what happens to the workspace disk; this choice is recorded on the
      operation and cannot change afterwards.
    </p>
    <fieldset className="mt-4 space-y-3">
      <legend className="sr-only">Workspace retention</legend>
      <label className="flex items-start gap-2">
        <input type="radio" name="workspace_retention" value="retain" checked={choice === 'retain'} onChange={() => setChoice('retain')} />
        <span><strong>Retain the workspace</strong> — the encrypted disk and any snapshots stay attributed to you and keep
          counting against your capacity and storage cost until deleted.</span>
      </label>
      <label className="flex items-start gap-2">
        <input type="radio" name="workspace_retention" value="delete" checked={choice === 'delete'} onChange={() => setChoice('delete')} />
        <span><strong>Delete the workspace</strong> — the disk is removed after the machine is verified terminated. This
          cannot be undone.</span>
      </label>
    </fieldset>
    <ModalFooter>
      <Button type="button" variant="outline" onClick={close} disabled={busy}>Cancel</Button>
      <Button type="button" variant="danger" disabled={choice === null || busy} isLoading={busy}
        onClick={() => { if (choice) onConfirm(choice); }}>
        Release
      </Button>
    </ModalFooter>
  </Modal>;
}
