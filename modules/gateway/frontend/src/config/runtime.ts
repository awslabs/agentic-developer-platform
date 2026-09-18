/** Public deployment settings. Runtime values take precedence over local Vite settings. */
declare global {
  interface Window {
    __ADP_CONFIG__?: Record<string, string>;
  }
}

export function deploymentSetting(name: string): string | undefined {
  if (typeof window !== 'undefined' && window.__ADP_CONFIG__) {
    // A runtime-configured release must never fall back to another account's build settings.
    return window.__ADP_CONFIG__[name];
  }
  return import.meta.env[name];
}
