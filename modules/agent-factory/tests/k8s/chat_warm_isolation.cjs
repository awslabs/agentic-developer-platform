const assert = require('node:assert/strict');
const { withSandboxPod } = require('../../agent/src/complex-task-chat/sandbox-launcher.ts');

const binding = { clusterIP: '10.100.42.17', caConfigMap: 'chat-sandbox-gateway-ca-aaaaaaaaaaaaaaaa' };
const image = `registry.example.test/chat-sandbox@sha256:${'a'.repeat(64)}`;
const livePods = new Map();
const createdPods = [];
const removedPods = [];

const api = {
  resolveGateway: async () => binding,
  create: async template => {
    const name = `${template.metadata.generateName}${createdPods.length + 1}`;
    const uid = `00000000-0000-0000-0000-${String(createdPods.length + 1).padStart(12, '0')}`;
    assert.equal(template.spec.serviceAccountName, 'adp-chat-sandbox');
    assert.equal(template.spec.automountServiceAccountToken, false);
    assert.equal(template.spec.containers[0].image, image);
    assert.equal(template.spec.containers[0].imagePullPolicy, 'IfNotPresent');
    assert.deepEqual(template.spec.volumes[1].emptyDir, { sizeLimit: '512Mi' });
    assert.equal(template.spec.volumes[0].projected.sources[0].serviceAccountToken.audience, 'adp-agent-bootstrap');
    assert.equal(template.spec.priorityClassName, undefined);
    assert(!JSON.stringify(template).match(/AWS_ROLE_ARN|AWS_SECRET_ACCESS_KEY|envFrom|hostPath/));
    assert.equal(livePods.size, 0);
    const identity = { name, uid };
    createdPods.push({ ...identity, labels: template.metadata.labels });
    livePods.set(name, { uid, files: new Set(), processes: new Set(), token: `mock-${uid}` });
    return { metadata: { ...template.metadata, ...identity }, spec: template.spec };
  },
  remove: async (name, uid) => {
    assert.equal(livePods.get(name)?.uid, uid);
    livePods.delete(name);
    removedPods.push({ name, uid });
  },
};

async function verifyIsolation() {
  const seenTokens = new Set();
  for (const [session, turn] of [['A', 1], ['A', 2], ['B', 1]]) {
    const runId = `session-${session}-turn-${turn}`;
    await withSandboxPod({ runId, image, gatewayUrl: 'https://gateway.example.test' }, api, async pod => {
      const workspace = livePods.get(pod.name);
      assert.equal(workspace.uid, pod.uid);
      assert.equal(workspace.files.size, 0);
      assert.equal(workspace.processes.size, 0);
      assert(!seenTokens.has(workspace.token));
      workspace.files.add(`canary-${session}-${turn}`);
      workspace.processes.add(`process-${session}-${turn}`);
      seenTokens.add(workspace.token);
    });
    assert.equal(livePods.size, 0);
  }
  assert.equal(createdPods.length, 3);
  assert.deepEqual(removedPods, createdPods.map(({ name, uid }) => ({ name, uid })));
  assert.equal(new Set(createdPods.map(pod => pod.name)).size, 3);
  assert.equal(new Set(createdPods.map(pod => pod.uid)).size, 3);
  assert.equal(new Set(createdPods.map(pod => pod.labels['adp.io/run-hash'])).size, 3);
}

verifyIsolation().catch(error => { console.error(error); process.exitCode = 1; });
