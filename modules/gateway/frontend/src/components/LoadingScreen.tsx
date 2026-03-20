export function LoadingScreen() {
  return (
    <div
      className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900"
      role="status"
      aria-label="Loading"
    >
      <div className="text-center">
        <div className="inline-block animate-spin rounded-full h-12 w-12 border-4 border-primary-200 border-t-primary-600 mb-4"></div>
        <p className="text-gray-600 dark:text-gray-400">Loading...</p>
      </div>
    </div>
  );
}

export function LoadingSkeleton({ className = '' }: { className?: string }) {
  return (
    <div
      className={`animate-pulse bg-gray-200 dark:bg-gray-700 rounded ${className}`}
      aria-hidden="true"
    />
  );
}

export function CardSkeleton() {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
      <LoadingSkeleton className="h-4 w-1/3 mb-4" />
      <LoadingSkeleton className="h-8 w-2/3 mb-2" />
      <LoadingSkeleton className="h-4 w-1/2" />
    </div>
  );
}

export function TableSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
      <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
        <LoadingSkeleton className="h-4 w-1/4" />
      </div>
      <div className="divide-y divide-gray-200 dark:divide-gray-700">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="px-6 py-4 flex gap-4">
            <LoadingSkeleton className="h-4 w-1/4" />
            <LoadingSkeleton className="h-4 w-1/3" />
            <LoadingSkeleton className="h-4 w-1/6" />
            <LoadingSkeleton className="h-4 w-1/6" />
          </div>
        ))}
      </div>
    </div>
  );
}
