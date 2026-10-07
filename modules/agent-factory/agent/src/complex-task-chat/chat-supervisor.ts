import { SQSClient, ReceiveMessageCommand, DeleteMessageCommand, type Message } from '@aws-sdk/client-sqs';
import { fromTokenFile } from '@aws-sdk/credential-provider-web-identity';
import { InClusterSandboxPodApi, type SandboxPodApi } from './sandbox-launcher';
import { registeredSandboxAssignment, type SupervisorTiming } from './sandbox-supervisor-admission';
import { reconcileSupervisorDelivery } from './sandbox-supervisor-completion';

type Settings = {
  roleArn: string;
  workerRoleArn?: string;
  tokenFile: string;
  region: string;
  queueUrl: string;
  image: string;
  gatewayUrl: string;
};

type Queue = { receive(): Promise<Message[]>; acknowledge(receipt: string): Promise<void> };

function validateSettings(settings: Settings): void {
  const role = /^arn:(aws|aws-us-gov|aws-cn):iam::([0-9]{12}):role\/[A-Za-z0-9/+=,.@_-]+$/.exec(settings.roleArn);
  if (!role || settings.roleArn === settings.workerRoleArn ||
      !/^\/var\/run\/secrets\/[A-Za-z0-9_./-]+$/.test(settings.tokenFile) ||
      settings.tokenFile.split('/').includes('..') || !/^[a-z0-9-]+$/.test(settings.region)) {
    throw new Error('Chat supervisor requires a dedicated projected identity');
  }
  const domain = role[1] === 'aws-cn' ? 'amazonaws.com.cn' : 'amazonaws.com';
  const queue = new URL(settings.queueUrl);
  if (queue.protocol !== 'https:' || queue.host !== `sqs.${settings.region}.${domain}` ||
      queue.username || queue.password || queue.search || queue.hash ||
      !new RegExp(`^/${role[2]}/[A-Za-z0-9_-]+\\.fifo$`).test(queue.pathname)) {
    throw new Error('Chat supervisor requires its account-bound FIFO queue');
  }
  registeredSandboxAssignment('{"message_id":"preflight","session_id":"preflight","task_id":"preflight","session_generation":1}',
    settings.image, settings.gatewayUrl);
}

export function createSupervisorQueue(settings: Settings, load: typeof fromTokenFile = fromTokenFile): Queue {
  validateSettings(settings);
  const client = new SQSClient({ region: settings.region, credentials: load({ roleArn: settings.roleArn,
    webIdentityTokenFile: settings.tokenFile, clientConfig: { region: settings.region } }) });
  return { receive: async () => (await client.send(new ReceiveMessageCommand({
    QueueUrl: settings.queueUrl, MaxNumberOfMessages: 1, WaitTimeSeconds: 20, VisibilityTimeout: 900,
  }))).Messages ?? [], acknowledge: async receipt => {
    if (typeof receipt !== 'string' || !receipt) throw new Error('Chat supervisor queue receipt invalid');
    await client.send(new DeleteMessageCommand({ QueueUrl: settings.queueUrl, ReceiptHandle: receipt }));
  } };
}

export async function runOneSupervisorDispatch(
  settings: Settings,
  queue: Queue,
  api: SandboxPodApi,
  send: typeof fetch = fetch,
  load: typeof fromTokenFile = fromTokenFile,
  timing: SupervisorTiming = {},
): Promise<'idle' | 'completed'> {
  validateSettings(settings);
  const messages = await queue.receive();
  if (!Array.isArray(messages) || messages.length > 1) throw new Error('Chat supervisor queue delivery invalid');
  if (!messages.length) return 'idle';
  const message = messages[0];
  if (typeof message.Body !== 'string' || typeof message.ReceiptHandle !== 'string' || !message.ReceiptHandle) {
    throw new Error('Chat supervisor queue delivery invalid');
  }
  const assignment = registeredSandboxAssignment(message.Body, settings.image, settings.gatewayUrl);
  await reconcileSupervisorDelivery(assignment, api, settings, send, load, timing);
  await queue.acknowledge(message.ReceiptHandle);
  return 'completed';
}

if (require.main === module) {
  const settings: Settings = {
    roleArn: process.env.ADP_CHAT_SUPERVISOR_ROLE_ARN ?? '',
    workerRoleArn: process.env.ADP_CHAT_WORKER_ROLE_ARN,
    tokenFile: process.env.AWS_WEB_IDENTITY_TOKEN_FILE ?? '',
    region: process.env.AWS_REGION ?? '',
    queueUrl: process.env.ADP_CHAT_SUPERVISOR_QUEUE_URL ?? '',
    image: process.env.ADP_CHAT_SANDBOX_IMAGE ?? '',
    gatewayUrl: process.env.ADP_CHAT_DATA_URL ?? '',
  };
  if (settings.roleArn !== process.env.AWS_ROLE_ARN ||
      ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'].some(key => process.env[key] !== undefined)) {
    throw new Error('Chat supervisor projected identity unavailable');
  }
  runOneSupervisorDispatch(settings, createSupervisorQueue(settings), new InClusterSandboxPodApi())
    .catch(error => { console.error((error as Error).message); process.exitCode = 1; });
}
