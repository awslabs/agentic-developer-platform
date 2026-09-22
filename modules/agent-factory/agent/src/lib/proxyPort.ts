/** Shared worker/bridge/proxy default; deployments may override the port. */
export function proxyPort(): string {
  const configured = process.env.SIGV4_PROXY_PORT;
  if (!configured) return '9090';
  // This value is interpolated into the loopback bridge URL
  // (`http://127.0.0.1:${proxyPort()}/...`), where a non-numeric value is not a port at
  // all: "9090@evil.example.com" parses as userinfo + host and silently moves the
  // destination off loopback. Accept only a plain TCP port number.
  if (!/^[0-9]{1,5}$/.test(configured) || Number(configured) < 1 || Number(configured) > 65535) {
    throw new Error('SIGV4_PROXY_PORT must be a TCP port number between 1 and 65535');
  }
  return configured;
}
