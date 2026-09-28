import { BedrockAccountRouting } from '@/components/bedrock/BedrockAccountRouting';
import { usePermissions } from '@/hooks/usePermissions';

export default function ModelAccess() {
  const { isPlatformAdmin } = usePermissions();
  return <div className="space-y-6"><h1 className="text-2xl font-bold text-gray-900 dark:text-white">Model access</h1>{isPlatformAdmin() ? <BedrockAccountRouting /> : <p>Model routing is managed by a platform admin.</p>}</div>;
}
