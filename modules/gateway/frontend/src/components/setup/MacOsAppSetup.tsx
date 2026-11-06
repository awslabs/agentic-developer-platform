import type { ReactNode } from 'react';
import { Card, CopyButton, Tabs, TabsList, Tab, TabPanel } from '@/components/ui';

const CODE = 'bg-gray-100 dark:bg-gray-700 px-1 rounded font-mono';

function Command({ children }: { children: string }) {
  return (
    <div className="relative mt-2">
      <pre className="p-3 pr-20 overflow-x-auto bg-gray-100 dark:bg-gray-800 rounded-lg text-sm">{children}</pre>
      <div className="absolute top-2 right-2"><CopyButton value={children} /></div>
    </div>
  );
}

function Steps({ items }: { items: { title: string; body: ReactNode }[] }) {
  return (
    <div className="space-y-6 text-sm text-gray-700 dark:text-gray-300">
      {items.map(({ title, body }, index) => (
        <div key={title}>
          <h3 className="font-semibold text-gray-900 dark:text-white">{index + 1}. {title}</h3>
          <div className="mt-2 space-y-2">{body}</div>
        </div>
      ))}
    </div>
  );
}

export function MacOsAppSetup({ baseUrl, installStep, signInStep }: {
  baseUrl: string;
  installStep: ReactNode;
  signInStep: ReactNode;
}) {
  const sharedSteps = [
    { title: 'Install the adp CLI', body: installStep },
    { title: 'Sign in once', body: signInStep },
  ];

  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Set up a macOS app</h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
          Reuse your ADP login for compatible desktop apps and CLI tools. If ADP is already
          installed and you are signed in, continue to the app connection step.
        </p>
      </div>
      <Card>
        <Tabs defaultValue="claude-app">
          <TabsList className="flex-wrap">
            <Tab value="claude-app">Claude App</Tab>
            <Tab value="chatgpt-app">ChatGPT App</Tab>
          </TabsList>
          <TabPanel value="claude-app">
            <Steps items={[
              {
                title: 'Check your Claude App build',
                body: <p>
                  Install Claude App for macOS. Your build must expose settings for a custom
                  Anthropic Messages gateway and a credential helper. Confirm those settings
                  are available before continuing; Claude Code configuration does not configure
                  the desktop app. ADP setup also needs curl, jq and python3 on your PATH.
                </p>,
              },
              ...sharedSteps,
              {
                title: 'Prepare the Claude App connection',
                body: <>
                  <Command>adp claude-desktop setup</Command>
                  <p>
                    This creates a credential helper and prints your connection details. It
                    reuses your ADP login and refreshes tokens automatically. It does not change
                    Claude App settings.
                  </p>
                  <p>
                    The helper stays tied to the selected deployment and tenant. To change that
                    selection later, select the new deployment or tenant and rerun setup.
                  </p>
                </>,
              },
              {
                title: 'Apply the connection in Claude App',
                body: <>
                  <p>In your build's custom gateway settings, use the values printed by setup:</p>
                  <ul className="list-disc ml-5 space-y-1">
                    <li>Provider: Anthropic Messages.</li>
                    <li>Gateway base URL for this deployment: <code className={CODE}>{baseUrl}</code>.</li>
                    <li>Authentication: bearer credentials supplied by the printed absolute credential-helper path.</li>
                    <li>Models: use the entitled model list printed by setup.</li>
                  </ul>
                  <p>
                    Keep the helper configured so credentials can refresh. Do not paste an access
                    token into the app. If these settings are unavailable, ask your platform
                    administrator for a compatible build.
                  </p>
                </>,
              },
              {
                title: 'Verify the gateway and the app',
                body: <>
                  <Command>adp claude-desktop setup --check</Command>
                  <p>
                    This checks gateway authentication and lists the number of available models.
                    It does not verify Claude App. Open a new conversation in the app, send a
                    short message, and confirm the request appears in ADP's Log Viewer.
                  </p>
                </>,
              },
            ]} />
          </TabPanel>
          <TabPanel value="chatgpt-app">
            <Steps items={[
              {
                title: 'Check your ChatGPT App build',
                body: <p>
                  This guide applies to compatible Codex-based ChatGPT builds that support
                  custom model providers and read <code className={CODE}>~/.codex/config.toml</code>.
                  The app name alone does not establish compatibility. Confirm your build with
                  your platform administrator. ADP setup also needs curl, jq and python3 on your PATH.
                </p>,
              },
              ...sharedSteps,
              {
                title: 'Configure the desktop connection',
                body: <>
                  <p><strong>Manual desktop configuration is currently required.</strong></p>
                  <p>
                    Ask your platform administrator to connect your supported app build to the
                    selected ADP deployment using its supported credential mechanism. The
                    connection must refresh credentials and work when the app opens from Finder.
                    ADP does not yet provide an automatic ChatGPT desktop setup command.
                  </p>
                  <details>
                    <summary className="cursor-pointer text-primary-600 dark:text-primary-400">Desktop connection requirements</summary>
                    <ul className="list-disc ml-5 mt-2 space-y-1">
                      <li>Preserve existing app settings and use the intended ADP deployment and tenant.</li>
                      <li>For the local proxy connection, start the proxy automatically and keep the app's provider address current.</li>
                      <li>Supply the real local proxy capability to the app securely; a terminal-only environment export does not configure a Finder launch.</li>
                      <li><code className={CODE}>adp codex setup</code> prepares Codex configuration. <code className={CODE}>adp daemon install</code> keeps the macOS proxy running. Neither completes desktop app authentication.</li>
                    </ul>
                  </details>
                  <p>
                    No access token needs to be copied. If desktop configuration is unavailable,
                    the CLI tools tab provides the supported Codex CLI setup.
                  </p>
                </>,
              },
              {
                title: 'Verify a new app conversation',
                body: <p>
                  Fully quit and reopen the app after configuration. Send a short message in a
                  new conversation and confirm it appears in ADP's Log Viewer. Verify again after
                  restarting your Mac and after credential renewal before treating setup as complete.
                </p>,
              },
            ]} />
          </TabPanel>
        </Tabs>
      </Card>
    </section>
  );
}
