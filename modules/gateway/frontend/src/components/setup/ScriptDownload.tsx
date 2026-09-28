import { Card, Button } from '@/components/ui';

export interface ScriptDownloadProps {
  scriptName: string;
  description: string;
  platform: 'unix' | 'windows' | 'all';
  downloadUrl: string;
  version?: string;
}

export function ScriptDownload({
  scriptName,
  description,
  platform,
  downloadUrl,
  version,
}: ScriptDownloadProps) {
  const getPlatformIcon = () => {
    switch (platform) {
      case 'unix':
        return '🐧';
      case 'windows':
        return '🪟';
      default:
        return '💻';
    }
  };

  const getPlatformLabel = () => {
    switch (platform) {
      case 'unix':
        return 'Linux / macOS';
      case 'windows':
        return 'Windows';
      default:
        return 'All Platforms';
    }
  };

  const handleDownload = () => {
    window.open(downloadUrl, '_blank');
  };

  return (
    <Card className="flex items-start gap-4">
      <div className="text-3xl" aria-hidden="true">
        {getPlatformIcon()}
      </div>
      <div className="flex-1">
        <div className="flex items-center gap-2">
          <h3 className="font-semibold text-gray-900 dark:text-white">{scriptName}</h3>
          {version && (
            <span className="text-xs px-2 py-0.5 bg-gray-100 dark:bg-gray-700 rounded-full text-gray-600 dark:text-gray-400">
              v{version}
            </span>
          )}
        </div>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{description}</p>
        <p className="text-xs text-gray-400 dark:text-gray-500 mt-2">
          Platform: {getPlatformLabel()}
        </p>
      </div>
      <Button onClick={handleDownload} variant="outline">
        Download
      </Button>
    </Card>
  );
}

export function ScriptDownloadList({ files }: { files?: string[] }) {
  // Every entry is served by GET /api/cli/{script_name}
  // (modules/gateway/src/cli_download/routes.py) — the allowlist there and this
  // list must stay in step, or a card offers a download that 404s.
  //
  // Removed here: the legacy `bg-auth.sh` (deprecated SigV4 credential exchange)
  // and `bg-auth.ps1` — which had no source file in the repo at all, so that
  // button advertised a download that could never succeed.
  //
  // Issue #4156: bg-gateway-proxy.py was missing, so Codex users following the
  // `serve` flow could not get the second of the two files it needs.
  //
  // Issue #4852: `adp` and `install.sh` joined the list. They are last, not
  // first, because the /setup page's primary flow is the one-line `curl … | sh`
  // install — these cards are for reading the source before running it, and for
  // air-gapped machines where the curl cannot reach the gateway.
  //
  // `files` filters the cards to the calling tab's own needs: each setup tab is
  // self-contained, so the Claude Code tab must not advertise the Codex-only
  // proxy (and its python3 note). Omit it for the full list.
  const scripts: ScriptDownloadProps[] = [
    {
      scriptName: 'bg-cognito-auth.sh',
      description:
        'Cognito authentication helper for Linux/macOS. Claude Code calls it via apiKeyHelper to mint and auto-refresh your gateway token.',
      platform: 'unix',
      downloadUrl: '/api/cli/bg-cognito-auth.sh',
    },
    {
      scriptName: 'bg-gateway-proxy.py',
      description:
        'Codex only — local auth proxy started by `bg-cognito-auth.sh serve`. Save it next to the helper script; serve looks for it as a sibling. Needs only stdlib python3.',
      platform: 'unix',
      downloadUrl: '/api/cli/bg-gateway-proxy.py',
    },
    {
      scriptName: 'adp',
      description:
        'The adp CLI — one command for login, status and per-tool setup. Normally installed by install.sh; download it directly only if you want to read it first.',
      platform: 'unix',
      downloadUrl: '/api/cli/adp',
    },
    {
      scriptName: 'install.sh',
      description:
        'Installer for the adp CLI. Places adp and the two scripts above side by side in ~/.adp/bin and remembers this gateway URL. Run: sh install.sh --gateway-url <this gateway>.',
      platform: 'unix',
      downloadUrl: '/api/cli/install.sh',
    },
  ];

  const visible = files ? scripts.filter((script) => files.includes(script.scriptName)) : scripts;

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
        Download Helper Scripts
      </h2>
      <div className="space-y-3">
        {visible.map((script) => (
          <ScriptDownload key={script.scriptName} {...script} />
        ))}
      </div>
    </div>
  );
}
