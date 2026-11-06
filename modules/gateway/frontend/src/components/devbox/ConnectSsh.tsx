/**
 * ConnectSsh — Issue #8094, inline on the Dev Box page since 2026-10-26.
 *
 * The SSH route for developers who work in a terminal, VS Code or Kiro, shown next to the
 * launch button. Deliberately contains no call to `/cloud-desktops/{id}/ssh-access`: that route
 * mints 15-minute AWS credentials for the CLI's ProxyCommand, which fetches them per connection.
 * The browser only ever shows the alias and the commands.
 */
import { CopyButton } from '@/components/ui/CopyButton';
import type { DevBox } from '@/hooks/useDevBox';

/** Mirrors `host_alias()` in modules/gateway/cli/adp-devbox.py for a legacy/default deployment. */
export function sshAlias(box: Pick<DevBox, 'desktop_id' | 'name'>): string {
  const name = (box.name ?? '').toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/^-+|-+$/g, '') || box.desktop_id;
  return `adp-devbox-${name.slice(0, 100)}`;
}

export default function ConnectSsh({ box }: { box: DevBox }) {
  const target = box.name && box.name.trim() ? box.name : box.desktop_id;
  const alias = sshAlias(box);
  const setup = ['adp update', `adp devbox ssh-config ${target}`, `adp devbox ssh ${target}`].join('\n');
  const running = box.status === 'running' && box.observed_state === 'running';

  return <details open className="mt-6 border-t border-gray-200 pt-4 text-gray-700 dark:border-gray-700 dark:text-gray-300">
    <summary className="cursor-pointer font-semibold text-gray-900 dark:text-gray-100" aria-label="Connect via SSH">Connect via SSH — terminal, VS Code Remote-SSH, Kiro</summary>
    <p className="mt-2 text-sm">
      No public IP, no bastion, no AWS keys of your own: the ADP CLI fetches a 15-minute grant for your box on every connection,
      pushes your SSH key for <code>ec2-user</code> and opens the session through AWS Systems Manager. You land in tmux with{' '}
      <code>gh</code>, Codex and Claude Code ready; your home and <code>/workspace</code> are on the box&apos;s retained disk.
    </p>
    {!running && <p role="note" className="mt-2 text-sm text-amber-700 dark:text-amber-300">
      The box must be running before a connection can be established.
    </p>}
    <h3 className="mt-3 font-medium text-gray-900 dark:text-gray-100">On your laptop, once</h3>
    <ul className="list-disc pl-5 text-sm">
      <li>Install OpenSSH (with <code>ssh-keygen</code>), AWS CLI v2 and the Session Manager plugin. They only carry the temporary grant
        the CLI receives from ADP: do not run <code>aws configure</code> or supply personal AWS keys.</li>
      <li>Sign in to the ADP CLI (<code>adp login</code>, then <code>adp status</code>), then run:</li>
    </ul>
    <pre className="mt-1 overflow-x-auto rounded bg-gray-100 p-3 text-sm dark:bg-gray-900"><code data-testid="ssh-setup">{setup}</code></pre>
    <CopyButton value={setup} label="Copy commands" aria-label="Copy SSH setup commands" />
    <p className="mt-2 text-sm">
      <code>ssh-config</code> writes one <code>~/.ssh/config</code> entry, alias <code data-testid="ssh-alias">{alias}</code>, whose ProxyCommand
      fetches a fresh grant each time; nothing long-lived is stored on your laptop or on the box. Named deployments prefix the deployment
      name (for example <code>adp-devbox-dev-{target}</code>); the CLI prints the exact alias it wrote.
    </p>
    <h3 className="mt-3 font-medium text-gray-900 dark:text-gray-100">Every time</h3>
    <ul className="list-disc pl-5 text-sm">
      <li><code>adp devbox ssh {target}</code> for a terminal, or plain <code>ssh {alias}</code>.</li>
      <li>VS Code: Remote - SSH → <em>Connect to Host</em> → <code>{alias}</code>. Kiro IDE and Kiro Crew remote-host (SSH) mode use the same alias.</li>
      <li>On the first connection verify the host-key fingerprint against the value your operator supplies. The box keeps one fingerprint
        across Replaces, so a &quot;host key changed&quot; warning is a real problem: never bypass it.</li>
    </ul>
  </details>;
}
