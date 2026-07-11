/**
 * SSRF URL validation guard for agent-runtime HTTP clients.
 *
 * Validates base URLs before any fetch() to prevent Server-Side Request Forgery:
 * - Internal VPC probing (ALB, IMDS, internal services)
 * - API key exfiltration via crafted base URLs
 *
 * Usage: call validateBaseUrl() at client construction time (not per-request).
 * Per-request URLs built from a validated base + static paths are safe.
 *
 * Issue #3582: SSRF URL validation for agent-runtime HTTP clients
 */

export interface ValidateBaseUrlOptions {
  /** Allow http: protocol (only for internal cluster hosts). Default: false (https required). */
  allowHttp?: boolean;
  /** When set, require the URL host to match this exact value. Used for vault/gateway pinning. */
  pinHost?: string;
}

/**
 * Validate a base URL for SSRF safety. Returns the normalized origin string.
 *
 * @throws Error if the URL is invalid, uses a blocked protocol, or targets a blocked host/IP.
 */
export function validateBaseUrl(url: string, opts: ValidateBaseUrlOptions = {}): string {
  const { allowHttp = false, pinHost } = opts;

  // 1. Parse — reject garbage URLs
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`validateBaseUrl: invalid URL: ${url}`);
  }

  // 2. Protocol check
  if (parsed.protocol === 'https:') {
    // Always allowed
  } else if (parsed.protocol === 'http:' && allowHttp) {
    // Allowed only when explicitly opted in (internal cluster hosts)
  } else {
    throw new Error(
      `validateBaseUrl: blocked protocol "${parsed.protocol}" in URL: ${url}. ` +
        (allowHttp ? 'Only http: and https: are allowed.' : 'Only https: is allowed.'),
    );
  }

  // 3. Host/IP blocking — prevent loopback, metadata, link-local
  const hostname = parsed.hostname.toLowerCase();

  if (isBlockedHost(hostname)) {
    throw new Error(
      `validateBaseUrl: blocked host "${hostname}" in URL: ${url}. ` +
        'Loopback, metadata, and link-local addresses are not allowed.',
    );
  }

  // 4. Host pinning — exact match required when configured
  if (pinHost && hostname !== pinHost.toLowerCase()) {
    throw new Error(
      `validateBaseUrl: host "${hostname}" does not match pinned host "${pinHost}". URL: ${url}`,
    );
  }

  // 5. Return normalized origin (scheme + host + port)
  return parsed.origin;
}

/**
 * Check if a hostname is blocked (loopback, metadata, link-local, or private-unroutable).
 */
function isBlockedHost(hostname: string): boolean {
  // Loopback names
  if (hostname === 'localhost' || hostname === 'localhost.localdomain') {
    return true;
  }

  // IPv6 loopback
  if (hostname === '::1' || hostname === '[::1]') {
    return true;
  }

  // Strip IPv6 brackets for numeric checks
  const bare = hostname.startsWith('[') && hostname.endsWith(']')
    ? hostname.slice(1, -1)
    : hostname;

  // IPv4 numeric checks
  if (isIPv4(bare)) {
    return isBlockedIPv4(bare);
  }

  // IPv6 checks (expanded)
  if (bare.includes(':')) {
    return isBlockedIPv6(bare);
  }

  return false;
}

/**
 * Simple IPv4 detection (4 dot-separated decimal octets).
 */
function isIPv4(s: string): boolean {
  const parts = s.split('.');
  if (parts.length !== 4) return false;
  return parts.every(p => {
    const n = Number(p);
    return Number.isInteger(n) && n >= 0 && n <= 255 && p === String(n);
  });
}

/**
 * Check if an IPv4 address is in a blocked range.
 */
function isBlockedIPv4(ip: string): boolean {
  const octets = ip.split('.').map(Number);
  const [a, b] = octets;

  // 127.0.0.0/8 — loopback
  if (a === 127) return true;

  // 0.0.0.0 — unspecified
  if (a === 0 && b === 0 && octets[2] === 0 && octets[3] === 0) return true;

  // 169.254.0.0/16 — link-local / AWS IMDS
  if (a === 169 && b === 254) return true;

  return false;
}

/**
 * Check if an IPv6 address is blocked (loopback, link-local, unique-local EC2 metadata,
 * or IPv4-mapped addresses that embed a blocked IPv4).
 */
function isBlockedIPv6(ip: string): boolean {
  const normalized = ip.toLowerCase();

  // ::1 — loopback
  if (normalized === '::1') return true;

  // fe80::/10 — link-local
  if (normalized.startsWith('fe80:') || normalized.startsWith('fe80')) return true;

  // fd00:ec2::254 — EC2 IMDS IPv6 endpoint
  if (normalized.startsWith('fd00:ec2:')) return true;
  if (normalized === 'fd00:ec2::254') return true;

  // IPv4-mapped IPv6 — ::ffff:x.x.x.x or ::ffff:HHHH:LLLL (hex form from Node URL parser)
  if (normalized.startsWith('::ffff:')) {
    const mapped = normalized.slice(7); // strip "::ffff:" prefix

    // Dotted-decimal form (e.g., ::ffff:127.0.0.1)
    if (isIPv4(mapped)) {
      return isBlockedIPv4(mapped);
    }

    // Hex-pair form (e.g., ::ffff:7f00:1 from Node's URL normalization)
    // Format is HHHH:LLLL where each is a 16-bit hex value encoding two octets
    const hexParts = mapped.split(':');
    if (hexParts.length === 2) {
      const hi = parseInt(hexParts[0], 16);
      const lo = parseInt(hexParts[1], 16);
      if (!isNaN(hi) && !isNaN(lo)) {
        const ipv4 = `${(hi >> 8) & 0xff}.${hi & 0xff}.${(lo >> 8) & 0xff}.${lo & 0xff}`;
        return isBlockedIPv4(ipv4);
      }
    }
  }

  return false;
}
