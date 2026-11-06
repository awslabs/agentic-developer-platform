import { LineIcon } from './LineIcon';

/** Decorative provider glyph. Keep a visible provider name beside it. */
export function ProviderIcon({ provider, className = 'h-5 w-5 shrink-0' }: {
  provider: string;
  className?: string;
}) {
  switch (provider.toLowerCase()) {
    case 'jira':
      return <svg viewBox="0 0 24 24" fill="currentColor" className={`${className} text-blue-600`} aria-hidden="true" focusable="false">
        <path fillRule="evenodd" d="M12 1 23 12 12 23 1 12 12 1Zm0 6-5 5 5 5 5-5-5-5Z" clipRule="evenodd" />
      </svg>;
    case 'github':
      return <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true" focusable="false">
        <path d="M12 0C5.374 0 0 5.373 0 12c0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23A11.509 11.509 0 0112 5.803c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576C20.566 21.797 24 17.3 24 12c0-6.627-5.373-12-12-12z" />
      </svg>;
    case 'aws':
      return <LineIcon className={`${className} text-amber-700`}>
        <path d="M7 18a5 5 0 1 1 .7-9.95A6 6 0 0 1 19 9.5a4.25 4.25 0 0 1-.25 8.5H7Z" />
      </LineIcon>;
    default:
      return <LineIcon className={className}>
        <path d="m10 13 4-4m-6 6-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 3 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(1 1)" />
      </LineIcon>;
  }
}
