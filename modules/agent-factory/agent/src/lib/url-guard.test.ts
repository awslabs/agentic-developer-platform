/**
 * Unit tests for SSRF URL validation guard.
 *
 * Issue #3582: SSRF URL validation for agent-runtime HTTP clients
 */

import { validateBaseUrl } from './url-guard';

describe('validateBaseUrl', () => {
  // ─── Valid URLs ──────────────────────────────────────────────────────────────

  describe('accepts valid URLs', () => {
    it('accepts a standard HTTPS URL', () => {
      expect(validateBaseUrl('https://gitlab.example.com')).toBe('https://gitlab.example.com');
    });

    it('accepts HTTPS URL with port', () => {
      expect(validateBaseUrl('https://gitlab.example.com:8443')).toBe(
        'https://gitlab.example.com:8443',
      );
    });

    it('accepts HTTPS URL with path (returns origin only)', () => {
      expect(validateBaseUrl('https://gitlab.example.com/api/v4')).toBe(
        'https://gitlab.example.com',
      );
    });

    it('accepts HTTP when allowHttp is true (internal cluster)', () => {
      expect(
        validateBaseUrl('http://bedrockgateway.adp-gateway:8080', { allowHttp: true }),
      ).toBe('http://bedrockgateway.adp-gateway:8080');
    });

    it('accepts internal K8s service URL with allowHttp', () => {
      expect(
        validateBaseUrl('http://context-mcp.agent-context.svc.cluster.local:5100', {
          allowHttp: true,
        }),
      ).toBe('http://context-mcp.agent-context.svc.cluster.local:5100');
    });

    it('accepts URL with pinHost when host matches', () => {
      expect(
        validateBaseUrl('http://bedrockgateway.adp-gateway:8080', {
          allowHttp: true,
          pinHost: 'bedrockgateway.adp-gateway',
        }),
      ).toBe('http://bedrockgateway.adp-gateway:8080');
    });

    it('accepts pinHost match case-insensitively', () => {
      expect(
        validateBaseUrl('https://GitLab.Example.COM', {
          pinHost: 'gitlab.example.com',
        }),
      ).toBe('https://gitlab.example.com');
    });
  });

  // ─── Protocol blocking ─────────────────────────────────────────────────────

  describe('blocks invalid protocols', () => {
    it('rejects HTTP when allowHttp is false (default)', () => {
      expect(() => validateBaseUrl('http://example.com')).toThrow(
        'blocked protocol "http:"',
      );
    });

    it('rejects file: protocol', () => {
      expect(() => validateBaseUrl('file:///etc/passwd')).toThrow('blocked protocol');
    });

    it('rejects ftp: protocol', () => {
      expect(() => validateBaseUrl('ftp://example.com')).toThrow('blocked protocol');
    });

    it('rejects javascript: protocol', () => {
      expect(() => validateBaseUrl('javascript:alert(1)')).toThrow(/invalid URL|blocked protocol/);
    });
  });

  // ─── Loopback blocking ─────────────────────────────────────────────────────

  describe('blocks loopback addresses', () => {
    it('rejects localhost', () => {
      expect(() => validateBaseUrl('https://localhost')).toThrow('blocked host');
    });

    it('rejects localhost with port', () => {
      expect(() => validateBaseUrl('https://localhost:8080')).toThrow('blocked host');
    });

    it('rejects 127.0.0.1', () => {
      expect(() => validateBaseUrl('https://127.0.0.1')).toThrow('blocked host');
    });

    it('rejects 127.0.0.1:9200', () => {
      expect(() => validateBaseUrl('https://127.0.0.1:9200')).toThrow('blocked host');
    });

    it('rejects 127.x.x.x variants', () => {
      expect(() => validateBaseUrl('https://127.255.0.1')).toThrow('blocked host');
    });

    it('rejects IPv6 loopback ::1', () => {
      expect(() => validateBaseUrl('https://[::1]')).toThrow('blocked host');
    });

    it('rejects 0.0.0.0', () => {
      expect(() => validateBaseUrl('https://0.0.0.0')).toThrow('blocked host');
    });
  });

  // ─── Metadata / link-local blocking ────────────────────────────────────────

  describe('blocks metadata and link-local addresses', () => {
    it('rejects AWS IMDS 169.254.169.254', () => {
      expect(() => validateBaseUrl('http://169.254.169.254/', { allowHttp: true })).toThrow(
        'blocked host',
      );
    });

    it('rejects 169.254.169.254 over HTTPS', () => {
      expect(() => validateBaseUrl('https://169.254.169.254')).toThrow('blocked host');
    });

    it('rejects 169.254.x.x range', () => {
      expect(() => validateBaseUrl('https://169.254.0.1')).toThrow('blocked host');
    });

    it('rejects EC2 IMDS IPv6 fd00:ec2::254', () => {
      expect(() => validateBaseUrl('http://[fd00:ec2::254]/', { allowHttp: true })).toThrow(
        'blocked host',
      );
    });

    it('rejects fe80:: link-local IPv6', () => {
      expect(() => validateBaseUrl('https://[fe80::1]')).toThrow('blocked host');
    });
  });

  // ─── IPv4-mapped IPv6 blocking ──────────────────────────────────────────────

  describe('blocks IPv4-mapped IPv6 addresses', () => {
    it('rejects ::ffff:127.0.0.1 (loopback via IPv4-mapped IPv6)', () => {
      expect(() =>
        validateBaseUrl('http://[::ffff:127.0.0.1]:8080', { allowHttp: true }),
      ).toThrow('blocked host');
    });

    it('rejects ::ffff:169.254.169.254 (IMDS via IPv4-mapped IPv6)', () => {
      expect(() =>
        validateBaseUrl('http://[::ffff:169.254.169.254]', { allowHttp: true }),
      ).toThrow('blocked host');
    });

    it('rejects ::ffff:0.0.0.0 (unspecified via IPv4-mapped IPv6)', () => {
      expect(() =>
        validateBaseUrl('http://[::ffff:0.0.0.0]', { allowHttp: true }),
      ).toThrow('blocked host');
    });

    it('rejects hex-form ::ffff:a9fe:a9fe (169.254.169.254 normalized by Node URL parser)', () => {
      // Node normalizes ::ffff:169.254.169.254 to ::ffff:a9fe:a9fe
      expect(() =>
        validateBaseUrl('http://[::ffff:a9fe:a9fe]', { allowHttp: true }),
      ).toThrow('blocked host');
    });

    it('accepts ::ffff: mapping of a public IP (e.g., 8.8.8.8)', () => {
      // ::ffff:8.8.8.8 normalized to ::ffff:808:808 — should pass
      expect(
        validateBaseUrl('https://[::ffff:8.8.8.8]'),
      ).toBe('https://[::ffff:808:808]');
    });
  });

  // ─── Host pinning ──────────────────────────────────────────────────────────

  describe('host pinning', () => {
    it('rejects URL when host does not match pinHost', () => {
      expect(() =>
        validateBaseUrl('https://evil.example.com', { pinHost: 'gateway.internal' }),
      ).toThrow('does not match pinned host');
    });

    it('rejects localhost even with allowHttp + pinHost pointing elsewhere', () => {
      expect(() =>
        validateBaseUrl('http://localhost:8080', {
          allowHttp: true,
          pinHost: 'bedrockgateway.adp-gateway',
        }),
      ).toThrow('blocked host');
    });
  });

  // ─── Invalid / garbage URLs ────────────────────────────────────────────────

  describe('rejects garbage URLs', () => {
    it('rejects empty string', () => {
      expect(() => validateBaseUrl('')).toThrow('invalid URL');
    });

    it('rejects non-URL string', () => {
      expect(() => validateBaseUrl('not-a-url')).toThrow('invalid URL');
    });

    it('rejects partial URL', () => {
      expect(() => validateBaseUrl('://missing-scheme')).toThrow('invalid URL');
    });
  });
});
