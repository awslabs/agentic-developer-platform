import { X509Certificate } from 'node:crypto';
import { bindSandboxGateway, validateGatewayBinding } from './sandbox-gateway-binding';
import { gatewayBinding, gatewayCa, gatewayCaPem, gatewayService } from './sandbox-gateway.fixture';

test('binds only the immutable public CA and dedicated Service address', () => {
  expect(bindSandboxGateway(gatewayService, gatewayCa, gatewayBinding.caConfigMap)).toEqual(gatewayBinding);
});

test.each([
  { ...gatewayCa, immutable: false },
  { ...gatewayCa, metadata: { ...gatewayCa.metadata, namespace: 'other' } },
  { ...gatewayCa, metadata: { ...gatewayCa.metadata, deletionTimestamp: '2026-10-06T00:00:00Z' } },
  { ...gatewayCa, data: { 'ca.crt': gatewayCaPem + '\n' } },
  { ...gatewayCa, data: { 'ca.crt': gatewayCaPem, 'tls.key': 'private-key' } },
  { ...gatewayCa, data: { 'ca.crt': gatewayCaPem + gatewayCaPem } },
  { ...gatewayCa, data: { 'ca.crt': 'not-a-certificate' } },
  { ...gatewayCa, binaryData: { 'tls.key': 'private-key' } },
  undefined,
])('refuses substituted or mutable trust material %#', ca => {
  expect(() => bindSandboxGateway(gatewayService, ca, gatewayBinding.caConfigMap)).toThrow();
});

test('refuses certificates that expire before the bounded turn ends', () => {
  const clock = jest.spyOn(Date, 'now').mockReturnValue(Date.parse(new X509Certificate(gatewayCaPem).validTo) - 1_000);
  try { expect(() => bindSandboxGateway(gatewayService, gatewayCa, gatewayBinding.caConfigMap)).toThrow('trust invalid'); }
  finally { clock.mockRestore(); }
});

test.each([
  { ...gatewayBinding, clusterIP: '169.254.169.254' },
  { ...gatewayBinding, clusterIP: '127.0.0.1' },
  { ...gatewayBinding, clusterIP: '10.0.0.999' },
  { ...gatewayBinding, caConfigMap: '../secrets/platform' },
  { ...gatewayBinding, caConfigMap: '' },
])('refuses an invalid launch binding %#', binding => {
  expect(() => validateGatewayBinding(binding)).toThrow();
});
