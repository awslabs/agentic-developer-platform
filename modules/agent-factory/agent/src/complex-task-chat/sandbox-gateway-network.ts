import { isIPv4 } from 'node:net';
import { isDeepStrictEqual } from 'node:util';

export const GATEWAY_SERVICE_PATH = '/api/v1/namespaces/adp-gateway/services/chat-sandbox-gateway';
export const GATEWAY_EGRESS_POLICY_NAME = 'chat-sandbox-gateway-egress';

export type NetworkPolicySpec = {
  podSelector?: {
    matchLabels?: Record<string, string>;
    matchExpressions?: Array<{ key: string; operator: string; values?: string[] }>;
  };
  policyTypes?: string[];
  ingress?: unknown[];
  egress?: unknown[];
};

const GATEWAY_SELECTOR = { 'app.kubernetes.io/name': 'chat-sandbox-gateway' };
const SANDBOX_SELECTOR = { matchLabels: { 'adp.io/chat-sandbox': 'true' } };

export function isPrivateIPv4(address: unknown): address is string {
  return typeof address === 'string' && isIPv4(address) &&
    (address.startsWith('10.') || address.startsWith('192.168.') || /^172\.(1[6-9]|2[0-9]|3[01])\./.test(address));
}

export function isGatewayEgress(spec: NetworkPolicySpec | undefined): boolean {
  return isDeepStrictEqual(spec?.podSelector, SANDBOX_SELECTOR) &&
    isDeepStrictEqual(spec?.policyTypes, ['Egress']) &&
    (spec?.ingress?.length ?? 0) === 0 &&
    isDeepStrictEqual(spec?.egress, [{
      to: [{
        namespaceSelector: { matchLabels: { 'kubernetes.io/metadata.name': 'adp-gateway' } },
        podSelector: { matchLabels: GATEWAY_SELECTOR },
      }],
      ports: [{ protocol: 'TCP', port: 8443 }],
    }]);
}

export function isSandboxDeny(spec: NetworkPolicySpec | undefined): boolean {
  return isDeepStrictEqual(spec?.podSelector, SANDBOX_SELECTOR) &&
    isDeepStrictEqual(spec?.policyTypes, ['Ingress', 'Egress']) &&
    (spec?.ingress?.length ?? 0) === 0 && (spec?.egress?.length ?? 0) === 0;
}

export function isDedicatedGatewayService(value: unknown): boolean {
  const service = value as {
    metadata?: { name?: string; namespace?: string; deletionTimestamp?: string };
    spec?: {
      type?: string; clusterIP?: string; clusterIPs?: string[]; selector?: Record<string, string>;
      ports?: unknown[]; externalName?: string; externalIPs?: string[]; loadBalancerIP?: string;
    };
  } | undefined;
  const spec = service?.spec;
  const address = spec?.clusterIP;
  return service?.metadata?.name === 'chat-sandbox-gateway' &&
    service.metadata.namespace === 'adp-gateway' && !service.metadata.deletionTimestamp &&
    spec?.type === 'ClusterIP' && isPrivateIPv4(address) &&
    isDeepStrictEqual(spec.clusterIPs, [address]) &&
    isDeepStrictEqual(spec.selector, GATEWAY_SELECTOR) &&
    isDeepStrictEqual(spec.ports, [{ name: 'https', protocol: 'TCP', port: 8443, targetPort: 8443 }]) &&
    !spec.externalName && !spec.loadBalancerIP && (spec.externalIPs?.length ?? 0) === 0;
}
