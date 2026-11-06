import { productPath } from '@/services/products';
import { productServices } from './services/productServiceNavigation';
import type { L5Service } from './services/L5ServiceJourney';

export const productWorkspaces = [
  ['profile', 'Profile'], ['build', 'Build'], ['release', 'Release'], ['operate', 'Operate'],
  ['insights', 'Insights'], ['agents', 'Agents'], ['knowledge', 'Knowledge'], ['budget', 'Budget & spend'],
] as const;
export type ProductWorkspace = typeof productWorkspaces[number][0];

export function workspaceFromPath(pathname: string): ProductWorkspace {
  const segment = pathname.replace(/\/+$/, '').split('/projects/')[1]?.split('/')[1];
  if (segment === 'flows') return 'build';
  if (segment === 'services') return 'agents';
  return productWorkspaces.some(([id]) => id === segment) ? segment as ProductWorkspace : 'profile';
}

export function workspacePath(productId: string, projectId: string, workspace: ProductWorkspace) {
  return productPath(productId, projectId) + (workspace === 'profile' ? '' : `/${workspace}`);
}

export function workspaceService(pathname: string): L5Service | undefined {
  if (!['agents', 'release', 'operate'].includes(workspaceFromPath(pathname))) return;
  const segment = pathname.replace(/\/+$/, '').split('/projects/')[1]?.split('/')[2];
  return productServices.find(service => service.id === segment)?.id;
}

export const agentPath = (productId: string, projectId: string, service: L5Service, workspace: 'agents' | 'release' | 'operate' = 'agents') =>
  `${workspacePath(productId, projectId, workspace)}/${service}`;
