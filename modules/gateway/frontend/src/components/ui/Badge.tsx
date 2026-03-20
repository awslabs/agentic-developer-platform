import type { ReactNode } from 'react';

export interface BadgeProps {
  children: ReactNode;
  variant?: 'default' | 'success' | 'warning' | 'danger' | 'info';
  size?: 'sm' | 'md';
  className?: string;
}

export function Badge({ children, variant = 'default', size = 'md', className = '' }: BadgeProps) {
  const variantStyles = {
    default: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300',
    success: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-300',
    warning: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-300',
    danger: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-300',
    info: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-300',
  };

  const sizeStyles = {
    sm: 'px-2 py-0.5 text-xs',
    md: 'px-2.5 py-0.5 text-sm',
  };

  return (
    <span
      className={`inline-flex items-center font-medium rounded-full ${variantStyles[variant]} ${sizeStyles[size]} ${className}`}
    >
      {children}
    </span>
  );
}

// Status badge with dot indicator
export function StatusBadge({
  status,
  label,
  className = '',
}: {
  status: 'healthy' | 'unhealthy' | 'unknown' | 'online' | 'offline';
  label?: string;
  className?: string;
}) {
  const statusConfig = {
    healthy: { color: 'bg-green-500', text: 'Healthy', variant: 'success' as const },
    unhealthy: { color: 'bg-red-500', text: 'Unhealthy', variant: 'danger' as const },
    unknown: { color: 'bg-gray-500', text: 'Unknown', variant: 'default' as const },
    online: { color: 'bg-green-500', text: 'Online', variant: 'success' as const },
    offline: { color: 'bg-red-500', text: 'Offline', variant: 'danger' as const },
  };

  const config = statusConfig[status];

  return (
    <Badge variant={config.variant} className={className}>
      <span className={`w-2 h-2 rounded-full ${config.color} mr-1.5`} aria-hidden="true" />
      {label || config.text}
    </Badge>
  );
}
