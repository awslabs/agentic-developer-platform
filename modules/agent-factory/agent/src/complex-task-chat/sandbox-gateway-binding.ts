import { createHash, X509Certificate } from 'node:crypto';
import { isDedicatedGatewayService, isPrivateIPv4 } from './sandbox-gateway-network';

export const SANDBOX_GATEWAY_HOST = 'chat-sandbox-gateway.adp-gateway.svc';
export const SANDBOX_GATEWAY_URL = `https://${SANDBOX_GATEWAY_HOST}:8443`;
export const SANDBOX_CA_FILE = '/var/run/adp-chat-ca/ca.crt';
export const SANDBOX_CA_NAME = /^chat-sandbox-gateway-ca-[a-f0-9]{16}$/;

export type SandboxGatewayBinding = { clusterIP: string; caConfigMap: string };

export function validateGatewayBinding(binding: SandboxGatewayBinding): void {
  if (!binding || !isPrivateIPv4(binding.clusterIP) || !SANDBOX_CA_NAME.test(binding.caConfigMap)) {
    throw new Error('Chat sandbox gateway binding invalid');
  }
}

export function bindSandboxGateway(service: unknown, value: unknown, caConfigMap: string): SandboxGatewayBinding {
  const config = value as {
    metadata?: { name?: string; namespace?: string; deletionTimestamp?: string };
    immutable?: boolean; data?: Record<string, string>; binaryData?: Record<string, string>;
  } | undefined;
  const pem = config?.data?.['ca.crt'];
  if (!isDedicatedGatewayService(service) || !SANDBOX_CA_NAME.test(caConfigMap) ||
      config?.metadata?.name !== caConfigMap || config.metadata.namespace !== 'adp-gateway-agents' ||
      config.metadata.deletionTimestamp || config.immutable !== true ||
      Object.keys(config.data ?? {}).join() !== 'ca.crt' || Object.keys(config.binaryData ?? {}).length !== 0 ||
      typeof pem !== 'string' || pem.length > 16_384 ||
      !/^-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\r?\n?$/.test(pem) ||
      caConfigMap !== `chat-sandbox-gateway-ca-${createHash('sha256').update(pem).digest('hex').slice(0, 16)}`) {
    throw new Error('Chat sandbox gateway trust unavailable');
  }
  const certificate = new X509Certificate(pem);
  if (!certificate.ca || !certificate.checkIssued(certificate) || !certificate.verify(certificate.publicKey) ||
      Date.parse(certificate.validFrom) > Date.now() || Date.parse(certificate.validTo) <= Date.now() + 900_000) {
    throw new Error('Chat sandbox gateway trust invalid');
  }
  return { clusterIP: (service as { spec: { clusterIP: string } }).spec.clusterIP, caConfigMap };
}
