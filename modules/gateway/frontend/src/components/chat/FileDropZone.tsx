/**
 * FileDropZone — drag-and-drop file upload for the chat composer.
 *
 * Stage C (#186): User drags a file into the chat input area. The component:
 *   1. Calls upload-token (via WS action) to get a presigned PUT URL
 *   2. PUTs the file directly to S3
 *   3. Calls upload-complete (via WS action) to catalog the artifact
 *   4. Reports the artifact_id back to the parent for inclusion in the next message
 *
 * Max file size: 50 MB. Shows upload progress and error states.
 */

import { useCallback, useRef, useState, type DragEvent } from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PendingUpload {
  filename: string;
  artifactId: string;
  sizeBytes: number;
}

export interface FileDropZoneProps {
  /** WebSocket ref for sending upload-token/upload-complete actions. */
  wsRef: React.RefObject<WebSocket | null>;
  sessionId: string | null;
  /** Called when an upload completes successfully. */
  onUploadComplete: (upload: PendingUpload) => void;
  children: React.ReactNode;
}

interface UploadTokenResponse {
  upload_url?: string;
  s3_key?: string;
  task_id?: string;
  expires_in?: number;
  error?: string;
}

interface UploadCompleteResponse {
  artifact_id?: string;
  deduplicated?: boolean;
  error?: string;
}

type UploadState = 'idle' | 'dragging' | 'uploading' | 'error';

const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50 MB

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function FileDropZone({
  wsRef,
  sessionId,
  onUploadComplete,
  children,
}: FileDropZoneProps) {
  const [state, setState] = useState<UploadState>('idle');
  const [errorMessage, setErrorMessage] = useState('');
  const [uploadFileName, setUploadFileName] = useState('');
  const dragCounterRef = useRef(0);

  const handleDragEnter = useCallback((e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current++;
    if (e.dataTransfer.types.includes('Files')) {
      setState('dragging');
    }
  }, []);

  const handleDragLeave = useCallback((e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current--;
    if (dragCounterRef.current === 0) {
      setState(prev => (prev === 'dragging' ? 'idle' : prev));
    }
  }, []);

  const handleDragOver = useCallback((e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback(
    async (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      dragCounterRef.current = 0;

      const file = e.dataTransfer.files[0];
      if (!file) {
        setState('idle');
        return;
      }

      if (file.size > MAX_FILE_SIZE) {
        setState('error');
        setErrorMessage(`File too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Max: 50 MB.`);
        setTimeout(() => setState('idle'), 3000);
        return;
      }

      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || !sessionId) {
        setState('error');
        setErrorMessage('Not connected. Please try again.');
        setTimeout(() => setState('idle'), 3000);
        return;
      }

      await uploadFile(file);
    },
    [wsRef, sessionId],
  );

  const uploadFile = useCallback(
    async (file: File) => {
      setState('uploading');
      setUploadFileName(file.name);
      setErrorMessage('');

      try {
        const ws = wsRef.current;
        if (!ws || ws.readyState !== WebSocket.OPEN || !sessionId) {
          throw new Error('Not connected');
        }

        // Step 1: Request upload token via WS
        const tokenResponse = await sendWsRequest<UploadTokenResponse>(ws, {
          action: 'upload-token',
          session_id: sessionId,
          filename: file.name,
          content_type: file.type || 'application/octet-stream',
          size_bytes: file.size,
        });

        if (!tokenResponse.upload_url || !tokenResponse.s3_key) {
          throw new Error(tokenResponse.error || 'Failed to get upload token');
        }

        // Step 2: PUT file directly to S3 via presigned URL
        const putResponse = await fetch(tokenResponse.upload_url, {
          method: 'PUT',
          headers: { 'Content-Type': file.type || 'application/octet-stream' },
          body: file,
        });

        if (!putResponse.ok) {
          throw new Error(`S3 upload failed: ${putResponse.status}`);
        }

        // Step 3: Compute checksum and call upload-complete
        const checksum = await computeSha256(file);

        const completeResponse = await sendWsRequest<UploadCompleteResponse>(ws, {
          action: 'upload-complete',
          session_id: sessionId,
          task_id: tokenResponse.task_id,
          s3_key: tokenResponse.s3_key,
          filename: file.name,
          content_type: file.type || 'application/octet-stream',
          size_bytes: file.size,
          checksum,
        });

        if (!completeResponse.artifact_id) {
          throw new Error(completeResponse.error || 'Failed to record upload');
        }

        onUploadComplete({
          filename: file.name,
          artifactId: completeResponse.artifact_id!,
          sizeBytes: file.size,
        });

        setState('idle');
      } catch (err) {
        setState('error');
        setErrorMessage((err as Error).message);
        setTimeout(() => setState('idle'), 4000);
      }
    },
    [wsRef, sessionId, onUploadComplete],
  );

  return (
    <div
      className="relative"
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      data-testid="file-drop-zone"
    >
      {children}

      {/* Drag overlay */}
      {state === 'dragging' && (
        <div className="absolute inset-0 bg-primary-500/10 border-2 border-dashed border-primary-500 rounded-xl flex items-center justify-center z-10 pointer-events-none">
          <div className="text-primary-600 dark:text-primary-400 text-sm font-medium">
            Drop file to attach
          </div>
        </div>
      )}

      {/* Upload indicator */}
      {state === 'uploading' && (
        <div className="absolute bottom-0 left-0 right-0 bg-blue-50 dark:bg-blue-900/20 border-t border-blue-200 dark:border-blue-800 px-3 py-1.5 text-xs text-blue-700 dark:text-blue-300 flex items-center gap-2 z-10">
          <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          Uploading {uploadFileName}...
        </div>
      )}

      {/* Error indicator */}
      {state === 'error' && errorMessage && (
        <div className="absolute bottom-0 left-0 right-0 bg-red-50 dark:bg-red-900/20 border-t border-red-200 dark:border-red-800 px-3 py-1.5 text-xs text-red-700 dark:text-red-300 z-10">
          {errorMessage}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Send a WS message and wait for the response. The ingest Lambda returns the
 * response synchronously as the WebSocket reply to the action.
 */
function sendWsRequest<T>(
  ws: WebSocket,
  payload: Record<string, unknown>,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      ws.removeEventListener('message', handler);
      reject(new Error('Upload request timed out'));
    }, 30_000);

    function handler(event: MessageEvent) {
      try {
        const data = JSON.parse(event.data);
        // Match response by action echo or by presence of expected fields
        if (
          data.upload_url !== undefined ||
          data.artifact_id !== undefined ||
          data.error !== undefined
        ) {
          clearTimeout(timeout);
          ws.removeEventListener('message', handler);
          resolve(data as T);
        }
      } catch {
        // Not our response — ignore
      }
    }

    ws.addEventListener('message', handler);
    ws.send(JSON.stringify(payload));
  });
}

/** Compute SHA-256 hex digest of a File using SubtleCrypto. */
async function computeSha256(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const hashBuffer = await crypto.subtle.digest('SHA-256', buffer);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
}
