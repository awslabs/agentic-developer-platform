import { readFileSync } from 'node:fs';

const values = {
  SUPERVISOR_IMAGE: process.env.SUPERVISOR_IMAGE,
  SUPERVISOR_ROLE_ARN: process.env.SUPERVISOR_ROLE_ARN,
  AWS_REGION: process.env.AWS_REGION,
  CHAT_FIFO_URL: process.env.CHAT_FIFO_URL,
  SANDBOX_IMAGE_DIGEST: process.env.SANDBOX_IMAGE_DIGEST,
  GATEWAY_HTTPS_ORIGIN: process.env.GATEWAY_HTTPS_ORIGIN,
  SANDBOX_CA_CONFIGMAP: process.env.SANDBOX_CA_CONFIGMAP,
  CREW_DASHBOARD_ORIGIN: process.env.ADP_CREW_DASHBOARD_ORIGIN ?? '',
  // Explicit sandbox start deadline (#8059); the launcher accepts 5 s..300 s.
  SANDBOX_START_DEADLINE_MS: process.env.ADP_CHAT_SANDBOX_START_DEADLINE_MS || '120000',
  // Deployment default for the persistent-session executor (#8115), resolved from SSM
  // chat-persistent-executor by deploy-chat-scaledjob.sh; a session's own choice outranks it.
  PERSISTENT_EXECUTOR: process.env.ADP_CHAT_PERSISTENT_EXECUTOR || 'sandbox-loop',
};
const executors = ['sandbox-loop', 'claude-code', 'kirocrew'];
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
  // The supervisor's gateway target is either an https origin or the API Gateway
  // stage URL (https://<id>.execute-api.<region>.amazonaws.com/<stage>) whose
  // AWS_IAM /internal/{proxy+} route fronts the gateway's internal plane. The
  // region must be the supervisor's own (SigV4 scope); never the sandbox listener.
  const gateway = new URL(values.GATEWAY_HTTPS_ORIGIN ?? '');
  const stageUrl = new RegExp(`^[a-z0-9]+\\.execute-api\\.${values.AWS_REGION}\\.amazonaws\\.com$`).test(gateway.hostname) &&
    /^\/[A-Za-z0-9_-]+$/.test(gateway.pathname) && !gateway.port &&
    `${gateway.origin}${gateway.pathname}` === values.GATEWAY_HTTPS_ORIGIN;
  if (gateway.protocol !== 'https:' || (gateway.origin !== values.GATEWAY_HTTPS_ORIGIN && !stageUrl) ||
      gateway.username || gateway.password || gateway.search || gateway.hash ||
      !/^[a-z0-9.-]+$/.test(gateway.hostname) ||
      /^chat-sandbox-gateway(?:\.|$)/.test(gateway.hostname)) {
    throw new Error('Supervisor requires a gateway HTTPS origin or API Gateway stage URL');
  }
  if (values.CREW_DASHBOARD_ORIGIN) {
    const origin = new URL(values.CREW_DASHBOARD_ORIGIN);
    if (origin.protocol !== 'https:' || origin.origin !== values.CREW_DASHBOARD_ORIGIN ||
        origin.pathname !== '/' || origin.search || origin.hash || !/^[a-z0-9.-]+$/.test(origin.hostname)) {
      throw new Error('Crew dashboard origin must be HTTPS');
    }
  }
  if (!/^[0-9]{4,6}$/.test(values.SANDBOX_START_DEADLINE_MS) ||
      Number(values.SANDBOX_START_DEADLINE_MS) < 5000 || Number(values.SANDBOX_START_DEADLINE_MS) > 300000) {
    throw new Error('Sandbox start deadline must be 5000..300000 ms');
  }
  if (!executors.includes(values.PERSISTENT_EXECUTOR)) {
    throw new Error('Persistent chat executor must be sandbox-loop, claude-code or kirocrew');
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
