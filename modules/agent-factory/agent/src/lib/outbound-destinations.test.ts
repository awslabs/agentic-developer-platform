import { validateBaseUrl } from './url-guard';
import { proxyPort } from './proxyPort';

describe('the loopback bridge port cannot move the destination off loopback', () => {
  const env = process.env;

  afterEach(() => {
    process.env = env;
  });

  const bridgeHost = (port: string): string =>
    new URL(`http://127.0.0.1:${port}/__run/knowledge`).hostname;

  it('relocates the bridge URL when a non-numeric port is interpolated', () => {
    // Why proxyPort() must validate: the value is pasted into a URL as text, and this
    // form parses as userinfo + host rather than as a port.
    expect(bridgeHost('9090@evil.example.com')).toBe('evil.example.com');
  });

  it.each(['9090', '8181', '1', '65535'])('accepts the plain TCP port %s', port => {
    process.env = { ...env, SIGV4_PROXY_PORT: port };

    expect(proxyPort()).toBe(port);
    expect(bridgeHost(proxyPort())).toBe('127.0.0.1');
  });

  it('defaults to 9090 when unset', () => {
    process.env = { ...env };
    delete process.env.SIGV4_PROXY_PORT;

    expect(proxyPort()).toBe('9090');
  });

  it.each([
    '9090@evil.example.com',
    '9090/../x',
    '9090 ',
    '0x2382',
    '',
    '99999',
    '0',
    'abc',
  ])('refuses the non-port value %p', value => {
    process.env = { ...env, SIGV4_PROXY_PORT: value };

    if (value === '') {
      // Empty is indistinguishable from unset for env vars; it falls back to the default.
      expect(proxyPort()).toBe('9090');
    } else {
      expect(() => proxyPort()).toThrow(/TCP port number/);
    }
  });
});

describe('the shared URL guard rejects credentials embedded in a destination', () => {
  it.each([
    'https://user:pass@evil.example.com',
    'https://user@evil.example.com',
    'https://context-mcp.agent-context.svc.cluster.local:5100@evil.example.com',
  ])('rejects %s', url => {
    expect(() => validateBaseUrl(url, { allowHttp: true })).toThrow(/credentials in URL/);
  });

  it.each([
    'https://user:sup3rsecret@evil.example.com',
    '  https://user:sup3rsecret@evil.example.com',
    'https:user:sup3rsecret@evil.example.com',
    'https://user:prefix@sup3rsecret@evil.example.com',
    'http://user:sup3rsecret@evil.example.com',
    'https://user:sup3rsecret@',
    'https://127.0.0.1/?token=sup3rsecret',
    'https://evil.example.com/?token=sup3rsecret',
  ])('does not expose credentials in validation errors for %s', url => {
    expect(() => validateBaseUrl(url, { pinHost: 'gateway.example.com' })).toThrow();
    expect(() => validateBaseUrl(url, { pinHost: 'gateway.example.com' })).not.toThrow(/sup3rsecret/);
  });

  // The guard must keep admitting the real internal destinations it protects.
  it.each([
    ['http://context-mcp.agent-context.svc.cluster.local:5100', 'http://context-mcp.agent-context.svc.cluster.local:5100'],
    ['https://gateway.execute-api.us-east-1.amazonaws.com', 'https://gateway.execute-api.us-east-1.amazonaws.com'],
    ['https://api.github.com', 'https://api.github.com'],
  ])('still admits the configured internal destination %s', (url, origin) => {
    expect(validateBaseUrl(url, { allowHttp: true })).toBe(origin);
  });

  it('still blocks the instance-metadata address', () => {
    expect(() => validateBaseUrl('http://169.254.169.254', { allowHttp: true })).toThrow(
      /blocked host/,
    );
  });
});
