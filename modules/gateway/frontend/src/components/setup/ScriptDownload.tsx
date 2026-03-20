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
  const scripts: ScriptDownloadProps[] = [
    {
      scriptName: 'bg-auth.sh',
      description: 'Background authentication helper script for Linux/macOS. Handles AWS SSO authentication and token refresh automatically.',
      platform: 'unix',
      downloadUrl: '/api/cli/bg-auth.sh',
      version: '1.0.0',
    },
    {
      scriptName: 'bg-auth.ps1',
      description: 'Background authentication helper script for Windows PowerShell. Handles AWS SSO authentication and token refresh automatically.',
      platform: 'windows',
      downloadUrl: '/api/cli/bg-auth.ps1',
      version: '1.0.0',
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
