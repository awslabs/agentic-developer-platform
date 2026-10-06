import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

function certificate(): string {
  const directory = mkdtempSync(join(tmpdir(), 'sandbox-ca-test-'));
  try {
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
      '-subj', '/CN=synthetic-sandbox-test', '-addext', 'basicConstraints=critical,CA:TRUE',
      '-keyout', join(directory, 'key.pem'), '-out', join(directory, 'ca.pem')], { stdio: 'ignore' });
    return readFileSync(join(directory, 'ca.pem'), 'utf8');
  } finally { rmSync(directory, { recursive: true, force: true }); }
}

export const gatewayCaPem = certificate();
export const gatewayBinding = { clusterIP: '10.100.42.17',
  caConfigMap: `chat-sandbox-gateway-ca-${createHash('sha256').update(gatewayCaPem).digest('hex').slice(0, 16)}` };
export const gatewayCa = { metadata: { name: gatewayBinding.caConfigMap, namespace: 'adp-gateway-agents' },
  immutable: true, data: { 'ca.crt': gatewayCaPem } };
export const gatewayService = { metadata: { name: 'chat-sandbox-gateway', namespace: 'adp-gateway' },
  spec: { type: 'ClusterIP', clusterIP: gatewayBinding.clusterIP, clusterIPs: [gatewayBinding.clusterIP],
    selector: { 'app.kubernetes.io/name': 'chat-sandbox-gateway' },
    ports: [{ name: 'https', protocol: 'TCP', port: 8443, targetPort: 8443 }] } };
