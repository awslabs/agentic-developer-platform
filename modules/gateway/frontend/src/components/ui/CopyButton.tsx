/**
 * CopyButton — copy a string to the clipboard with a confirmation state.
 *
 * Issue #4146. Extracted rather than added to: four independent copy
 * implementations already existed in this app (AgentManagement, CodeBlock,
 * and two others) with no shared primitive. The clipboard logic here is the
 * most robust of them — `CodeBlock`'s, which falls back to `execCommand` for
 * insecure contexts where `navigator.clipboard` is undefined.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, type ButtonProps } from './Button';

export interface CopyButtonProps extends Omit<ButtonProps, 'onClick' | 'children'> {
  /** The text written to the clipboard. */
  value: string;
  /** Idle label. Defaults to "Copy". */
  label?: string;
  /** Confirmation label shown briefly after a successful copy. */
  copiedLabel?: string;
}

export function CopyButton({
  value,
  label = 'Copy',
  copiedLabel = 'Copied',
  variant = 'secondary',
  size = 'sm',
  ...props
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Don't set state on an unmounted component if the row disappears mid-timeout.
  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  const confirm = useCallback(() => {
    setCopied(true);
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setCopied(false), 2000);
  }, []);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(value);
      confirm();
    } catch {
      // Fallback for insecure contexts (no navigator.clipboard).
      const textarea = document.createElement('textarea');
      textarea.value = value;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
      confirm();
    }
  }, [value, confirm]);

  return (
    <Button
      variant={variant}
      size={size}
      onClick={handleCopy}
      aria-label={copied ? copiedLabel : label}
      {...props}
    >
      {copied ? copiedLabel : label}
    </Button>
  );
}
