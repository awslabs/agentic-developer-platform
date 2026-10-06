import { readFileSync } from 'node:fs';

const image = process.env.SANDBOX_IMAGE_DIGEST ?? '';
const capacity = process.env.CHAT_WARM_CAPACITY ?? '';

if (!/^[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}$/.test(image) ||
    !/^[1-3]$/.test(capacity)) {
  process.stderr.write('Chat warming requires the approved sandbox image digest and capacity 1-3\n');
  process.exitCode = 1;
} else {
  const template = readFileSync(new URL('./chat-warm.yaml', import.meta.url), 'utf8');
  process.stdout.write(template.replaceAll('REPLACE_WITH_SANDBOX_IMAGE_DIGEST', image)
    .replaceAll('REPLACE_WITH_WARM_CAPACITY', capacity));
}
