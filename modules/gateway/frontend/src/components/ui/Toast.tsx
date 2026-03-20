// Toast component - mainly used through ToastContext
// This file provides standalone Toast components if needed

import type { ToastType } from '@/contexts/ToastContext';

export interface ToastProps {
  type: ToastType;
  message: string;
  onDismiss?: () => void;
}

export function Toast({ type, message, onDismiss }: ToastProps) {
  const bgColors: Record<ToastType, string> = {
    success: 'bg-green-500',
    error: 'bg-red-500',
    warning: 'bg-yellow-500',
    info: 'bg-blue-500',
  };

  const icons: Record<ToastType, string> = {
    success: '✓',
    error: '✕',
    warning: '⚠',
    info: 'ℹ',
  };

  return (
    <div
      className={`${bgColors[type]} text-white px-4 py-3 rounded-lg shadow-lg flex items-center gap-3 min-w-[300px] max-w-[400px]`}
      role="alert"
    >
      <span className="text-lg" aria-hidden="true">
        {icons[type]}
      </span>
      <span className="flex-1">{message}</span>
      {onDismiss && (
        <button
          onClick={onDismiss}
          className="text-white hover:text-gray-200 focus:outline-none"
          aria-label="Dismiss notification"
        >
          ✕
        </button>
      )}
    </div>
  );
}
