// GHSA-jqcg-44mw-7w3h: ambiguous IPv6 trust ranges must not trust every IPv4 peer.
export {};

const proxyaddr = require('proxy-addr');

function request(remoteAddress: string) {
  return { socket: { remoteAddress }, headers: { 'x-forwarded-for': '192.0.2.123' } };
}

test.each(['::ffff:10.0.0.0/8', '::/1'])(
  'ignores a spoofed forwarded address for the IPv6 trust range %s', subnet => {
    expect(proxyaddr(request('203.0.113.10'), proxyaddr.compile(subnet))).toBe('203.0.113.10');
  },
);

test.each(['10.0.0.0/8', '::ffff:10.0.0.0/104'])(
  'preserves forwarding through a correctly configured trusted proxy %s', subnet => {
    expect(proxyaddr(request('10.1.2.3'), proxyaddr.compile(subnet))).toBe('192.0.2.123');
    expect(proxyaddr(request('203.0.113.10'), proxyaddr.compile(subnet))).toBe('203.0.113.10');
  },
);
