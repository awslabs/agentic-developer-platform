import { readFileSync } from 'node:fs';

const values = {
  SUPERVISOR_IMAGE: process.env.SUPERVISOR_IMAGE,
  SUPERVISOR_ROLE_ARN: process.env.SUPERVISOR_ROLE_ARN,
  AWS_REGION: process.env.AWS_REGION,
  CHAT_FIFO_URL: process.env.CHAT_FIFO_URL,
  SANDBOX_IMAGE_DIGEST: process.env.SANDBOX_IMAGE_DIGEST,
  GATEWAY_HTTPS_ORIGIN: process.env.GATEWAY_HTTPS_ORIGIN,
  SANDBOX_CA_CONFIGMAP: process.env.SANDBOX_CA_CONFIGMAP,
};
const imagePattern = /^[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}$/;
const rolePattern = /^arn:(aws|aws-us-gov|aws-cn):iam::([0-9]{12}):role\/adp-[a-z0-9-]+-chat-supervisor-role$/;

try {
  const role = rolePattern.exec(values.SUPERVISOR_ROLE_ARN ?? '');
  if (!role || !imagePattern.test(values.SUPERVISOR_IMAGE ?? '') ||
      !imagePattern.test(values.SANDBOX_IMAGE_DIGEST ?? '') ||
      values.SUPERVISOR_IMAGE === values.SANDBOX_IMAGE_DIGEST ||
      !/^chat-sandbox-gateway-ca-[a-f0-9]{16}$/.test(values.SANDBOX_CA_CONFIGMAP ?? '') ||
      !/^[a-z0-9-]+$/.test(values.AWS_REGION ?? '')) {
    throw new Error('Invalid supervisor identity or pinned images');
  }
  const suffix = role[1] === 'aws-cn' ? 'amazonaws.com.cn' : 'amazonaws.com';
  const queue = new URL(values.CHAT_FIFO_URL ?? '');
  if (queue.protocol !== 'https:' || queue.host !== `sqs.${values.AWS_REGION}.${suffix}` ||
      queue.username || queue.password || queue.search || queue.hash ||
      !new RegExp(`^/${role[2]}/[A-Za-z0-9_-]+\\.fifo$`).test(queue.pathname)) {
    throw new Error('Supervisor FIFO queue does not match its account and region');
  }
  const gateway = new URL(values.GATEWAY_HTTPS_ORIGIN ?? '');
  if (gateway.protocol !== 'https:' || gateway.origin !== values.GATEWAY_HTTPS_ORIGIN ||
      gateway.username || gateway.password || gateway.search || gateway.hash ||
      !/^[a-z0-9.-]+$/.test(gateway.hostname)) {
    throw new Error('Supervisor requires a gateway HTTPS origin');
  }
  let rendered = readFileSync(new URL('./chat-supervisor-job.yaml', import.meta.url), 'utf8');
  for (const [key, value] of Object.entries(values)) {
    const placeholder = `REPLACE_WITH_${key}`;
    if (!rendered.includes(placeholder)) throw new Error('Supervisor template placeholder missing');
    rendered = rendered.replaceAll(placeholder, value);
  }
  if (rendered.includes('REPLACE_WITH_')) throw new Error('Supervisor template has unresolved placeholders');
  process.stdout.write(rendered);
} catch {
  process.stderr.write('Supervisor Job rendering refused: missing or inconsistent scoped assignment\n');
  process.exitCode = 1;
}
