/** Shared worker/bridge/proxy default; deployments may override the port. */
export function proxyPort(): string {
  return process.env.SIGV4_PROXY_PORT || '9090';
}
