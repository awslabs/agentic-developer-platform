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

export function ScriptDownloadList() {
  // Issue #4146: one entry, served by GET /api/cli/{script_name}
  // (modules/gateway/src/cli_download/routes.py).
  //
  // Removed here: the legacy `bg-auth.sh` (deprecated SigV4 credential exchange)
  // and `bg-auth.ps1` — which had no source file in the repo at all, so that
  // button advertised a download that could never succeed.
  const scripts: ScriptDownloadProps[] = [
    {
      scriptName: 'bg-cognito-auth.sh',
      description:
        'Cognito authentication helper for Linux/macOS. Claude Code calls it via apiKeyHelper to mint and auto-refresh your gateway token.',
      platform: 'unix',
      downloadUrl: '/api/cli/bg-cognito-auth.sh',
    },
  ];

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
        Download Helper Scripts
      </h2>
      <div className="space-y-3">
        {scripts.map((script) => (
          <ScriptDownload key={script.scriptName} {...script} />
        ))}
      </div>
    </div>
  );
}
