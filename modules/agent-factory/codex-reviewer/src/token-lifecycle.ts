/** The shared worker owns broker policy, expiry and atomic token-file publication. */
interface TokenManager {
  getRuntimeGitHubToken(): Promise<string>;
}

export async function loadWorkerTokenManager(): Promise<TokenManager> {
  // Docker installs the existing CommonJS worker alongside this ESM adapter.
  const moduleUrl = new URL("../../dist/token-refresh.js", import.meta.url).href;
  const shared = await import(moduleUrl);
  const manager = shared.default ?? shared;
  if (typeof manager.getRuntimeGitHubToken !== "function") {
    throw new Error("Shared worker GitHub token manager unavailable");
  }
  return manager;
}

export async function withGitHubTokenRenewal<T>(
  run: (getToken: () => Promise<string>, initialToken: string) => Promise<T>,
  loadManager = loadWorkerTokenManager,
  intervalMs = 5 * 60 * 1000,
  warn: () => void = () => console.error("GitHub token renewal failed; next operation will retry through the shared worker broker"),
): Promise<T> {
  const manager = await loadManager();
  const getToken = () => manager.getRuntimeGitHubToken();
  // Fail closed for mediated runs/missing renewal configuration. PAT behavior
  // and the real bootstrap expiry are governed by the shared manager as well.
  const initialToken = await getToken();
  let pending: Promise<void> | undefined;
  const timer = setInterval(() => {
    if (!pending) {
      pending = getToken().then(() => {}, () => warn()).finally(() => { pending = undefined; });
    }
  }, intervalMs);
  timer.unref();
  try {
    return await run(getToken, initialToken);
  } finally {
    clearInterval(timer);
    await pending;
  }
}
